from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .analysis import load_jsonl, write_analysis
from .banks import apply_lure_bank, load_lure_bank
from .corridor import export_nested_zip, generate_corridor, load_corridor, save_corridor
from .env import load_dotenv
from .judge import KimiJudge, judge_jsonl
from .llm import LLMError, OpenAICompatibleClient
from .planner import corridor_from_plan, load_plan, save_plan
from .protocol import (
    LURE_MODES,
    MODES,
    PROTOCOL_VERSION,
    build_cases,
    parse_csv,
    parse_int_spec,
    resolve_models,
)
from .probes import run_probe_eval
from .runner import EvaluationRunner, RunConfig, append_jsonl
from .scoring import summarize_steps
from .search import run_evolutionary_search
from .smt import generate_smt_plan
from .surface import expand_plan_surface_local, expand_plan_surface_sdk_sync
from .synthesis import generate_lure_bank_local, generate_lure_bank_sdk_sync, generate_lure_bank_template
from .tactics import OBJECTIVES
from .workqueue import enqueue_planned_probe_jobs, queue_status, reclaim_stale_jobs, run_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corg-eval",
        description="Defensive eval harness for nested riddle corridors.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout-s", type=float, default=900.0)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("models", help="List models exposed by the OpenAI-compatible endpoint.")

    doctor = sub.add_parser("doctor", help="Check local model and optional Kimi judge configuration.")
    doctor.add_argument("--models", default="target", help="'target' or comma-separated model ids.")
    doctor.add_argument("--dotenv", default=".env")
    doctor.add_argument("--check-kimi", action="store_true")

    build = sub.add_parser("build", help="Generate a corridor JSON file and optional nested zip.")
    _add_corridor_generation_args(build)
    build.add_argument("--plan", default=None, help="Render a saved SMT corridor plan instead of template defaults.")
    build.add_argument("--out", default="corridors/example.json")
    build.add_argument("--zip-out", default=None)
    build.add_argument("--lure-bank", default=None)
    build.add_argument("--bank-arc", default=None)

    run = sub.add_parser("run", help="Run corridor eval trials.")
    run.add_argument("--model", required=True)
    run.add_argument("--corridor", default=None)
    run.add_argument("--plan", default=None)
    _add_corridor_generation_args(run)
    run.add_argument("--lure-bank", default=None)
    run.add_argument("--bank-arc", default=None)
    run.add_argument("--trials", type=int, default=1)
    run.add_argument("--mode", choices=["cumulative", "reset"], default="cumulative")
    run.add_argument("--compare-reset", action="store_true")
    run.add_argument("--defense", choices=["strict", "bare"], default="strict")
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--max-tokens", type=int, default=8192)
    run.add_argument("--extra-body-json", default=None, help="JSON object merged into each chat completion request.")
    run.add_argument("--no-think", action="store_true", help="Append /no_think for Qwen-style thinking models.")
    run.add_argument("--stop-on-failure", action="store_true")
    run.add_argument("--out", default="runs/corg_eval.jsonl")

    probe = sub.add_parser("probe", help="Run a corridor with pre/post persona and belief probes.")
    probe.add_argument("--model", required=True)
    probe.add_argument("--corridor", default=None)
    probe.add_argument("--plan", default=None)
    _add_corridor_generation_args(probe)
    probe.add_argument("--lure-bank", default=None)
    probe.add_argument("--bank-arc", default=None)
    probe.add_argument("--defense", choices=["strict", "bare"], default="strict")
    probe.add_argument("--temperature", type=float, default=0.0)
    probe.add_argument("--max-tokens", type=int, default=8192)
    probe.add_argument("--extra-body-json", default=None)
    probe.add_argument("--no-think", action="store_true")
    probe.add_argument("--out", default="runs/probe.jsonl")

    probe_plans = sub.add_parser("probe-plans", help="Run pre/post probes over SMT planned corridors.")
    probe_plans.add_argument("--models", default="target", help="'target' or comma-separated model ids.")
    probe_plans.add_argument("--seeds", default="300:301", help="Comma values and half-open ranges, e.g. 300:305.")
    probe_plans.add_argument("--objectives", default="mixed", help=f"Comma-separated objectives from {','.join(sorted(OBJECTIVES))}.")
    probe_plans.add_argument("--depth", type=int, default=4)
    probe_plans.add_argument("--warmup", type=int, default=1)
    probe_plans.add_argument("--min-conflict-step", type=int, default=2)
    probe_plans.add_argument("--surface-provider", choices=["template", "sdk", "local"], default="template")
    probe_plans.add_argument("--surface-model", default="google/gemma-4-31b")
    probe_plans.add_argument("--corpus-dir", default=None, help="Optional directory to persist generated plans and surfaced corridors.")
    probe_plans.add_argument("--defense", choices=["strict", "bare"], default="strict")
    probe_plans.add_argument("--temperature", type=float, default=0.0)
    probe_plans.add_argument("--surface-temperature", type=float, default=0.7)
    probe_plans.add_argument("--max-tokens", type=int, default=8192)
    probe_plans.add_argument("--surface-max-tokens", type=int, default=8192)
    probe_plans.add_argument("--extra-body-json", default=None)
    probe_plans.add_argument("--no-think", action="store_true")
    probe_plans.add_argument("--skip-missing", action="store_true")
    probe_plans.add_argument("--resume", action="store_true")
    probe_plans.add_argument("--out", default="runs/planned-probes.jsonl")

    queue_enqueue = sub.add_parser("queue-enqueue-plans", help="Enqueue planned-probe jobs in a filesystem queue.")
    queue_enqueue.add_argument("--queue-dir", default="queues/planned-pilot")
    queue_enqueue.add_argument("--models", default="target", help="'target' or comma-separated model ids.")
    queue_enqueue.add_argument("--seeds", default="410:411")
    queue_enqueue.add_argument("--objectives", default="mixed,corrigibility")
    queue_enqueue.add_argument("--depth", type=int, default=3)
    queue_enqueue.add_argument("--warmup", type=int, default=1)
    queue_enqueue.add_argument("--min-conflict-step", type=int, default=2)
    queue_enqueue.add_argument("--surface-provider", choices=["template", "sdk", "local"], default="sdk")
    queue_enqueue.add_argument("--surface-model", default="google/gemma-4-31b")
    queue_enqueue.add_argument("--surface-temperature", type=float, default=0.7)
    queue_enqueue.add_argument("--surface-max-tokens", type=int, default=8192)
    queue_enqueue.add_argument("--corpus-dir", default="corpora/planned-pilot-v0.1")
    queue_enqueue.add_argument("--defense", choices=["strict", "bare"], default="strict")
    queue_enqueue.add_argument("--temperature", type=float, default=0.0)
    queue_enqueue.add_argument("--max-tokens", type=int, default=8192)
    queue_enqueue.add_argument("--extra-body-json", default=None)
    queue_enqueue.add_argument("--no-think", action="store_true")
    queue_enqueue.add_argument("--out", default="runs/planned-pilot-queue.jsonl")

    queue_work = sub.add_parser("queue-work", help="Run jobs from a filesystem queue.")
    queue_work.add_argument("--queue-dir", default="queues/planned-pilot")
    queue_work.add_argument("--models", default="target", help="'target' or comma-separated model ids this worker may claim.")
    queue_work.add_argument("--worker-id", default=None)
    queue_work.add_argument("--max-jobs", type=int, default=1, help="0 means no limit.")
    queue_work.add_argument("--poll", action="store_true")
    queue_work.add_argument("--poll-interval-s", type=float, default=10.0)

    queue_status_parser = sub.add_parser("queue-status", help="Show filesystem queue counts.")
    queue_status_parser.add_argument("--queue-dir", default="queues/planned-pilot")
    queue_status_parser.add_argument("--stale-threshold-m", type=int, default=60)

    queue_reclaim = sub.add_parser("queue-reclaim", help="Move stale running jobs back to pending.")
    queue_reclaim.add_argument("--queue-dir", default="queues/planned-pilot")
    queue_reclaim.add_argument("--stale-threshold-m", type=int, default=60)

    benchmark = sub.add_parser("benchmark", help="Run the preregistered multi-model matrix.")
    benchmark.add_argument("--models", default="target", help="'target' or comma-separated model ids.")
    benchmark.add_argument("--seeds", default="0:3", help="Comma values and half-open ranges, e.g. 0:10.")
    benchmark.add_argument("--depths", default="2")
    benchmark.add_argument("--difficulties", default="1")
    benchmark.add_argument("--lure-strengths", default="2")
    benchmark.add_argument("--lure-modes", default="none,decoy,conflict")
    benchmark.add_argument("--modes", default="reset,cumulative")
    benchmark.add_argument("--defenses", default="strict")
    benchmark.add_argument("--temperature", type=float, default=0.0)
    benchmark.add_argument("--max-tokens", type=int, default=8192)
    benchmark.add_argument("--extra-body-json", default=None, help="JSON object merged into each chat completion request.")
    benchmark.add_argument("--no-think", action="store_true", help="Append /no_think for Qwen-style thinking models.")
    benchmark.add_argument("--skip-missing", action="store_true")
    benchmark.add_argument("--out", default="runs/benchmark.jsonl")

    generate_bank = sub.add_parser("generate-bank", help="Generate and persist benign lure arcs.")
    generate_bank.add_argument("--provider", choices=["sdk", "local", "template"], default="template")
    generate_bank.add_argument("--model", default="google/gemma-4-31b")
    generate_bank.add_argument("--count", type=int, default=3)
    generate_bank.add_argument("--depth", type=int, default=6)
    generate_bank.add_argument("--temperature", type=float, default=0.7)
    generate_bank.add_argument("--max-tokens", type=int, default=8192)
    generate_bank.add_argument("--out", default="banks/lure-bank.json")

    plan = sub.add_parser("plan", help="Generate a constrained corridor plan with z3.")
    plan.add_argument("--depth", type=int, default=6)
    plan.add_argument("--seed", type=int, default=0)
    plan.add_argument("--objective", choices=sorted(OBJECTIVES), default="mixed")
    plan.add_argument("--warmup", type=int, default=1)
    plan.add_argument("--min-conflict-step", type=int, default=2)
    plan.add_argument("--out", default="corridors/plan.json")

    surface_plan = sub.add_parser("surface-plan", help="Render an SMT plan through templates, Kimi, or a local model.")
    surface_plan.add_argument("--plan", required=True)
    surface_plan.add_argument("--provider", choices=["template", "sdk", "local"], default="template")
    surface_plan.add_argument("--model", default="google/gemma-4-31b")
    surface_plan.add_argument("--temperature", type=float, default=0.7)
    surface_plan.add_argument("--max-tokens", type=int, default=8192)
    surface_plan.add_argument("--out", default="corridors/surfaced.json")

    search = sub.add_parser("search", help="Run a simple evolutionary search over corridor variants.")
    search.add_argument("--model", required=True)
    search.add_argument("--generations", type=int, default=3)
    search.add_argument("--population", type=int, default=6)
    search.add_argument("--depth", type=int, default=6)
    search.add_argument("--mode", choices=["cumulative", "reset"], default="cumulative")
    search.add_argument("--defense", choices=["strict", "bare"], default="strict")
    search.add_argument("--temperature", type=float, default=0.0)
    search.add_argument("--max-tokens", type=int, default=8192)
    search.add_argument("--extra-body-json", default=None, help="JSON object merged into each chat completion request.")
    search.add_argument("--no-think", action="store_true", help="Append /no_think for Qwen-style thinking models.")
    search.add_argument("--seed", type=int, default=0)
    search.add_argument("--out", default="runs/corg_search.jsonl")

    summarize = sub.add_parser("summarize", help="Summarize one or more JSONL run files.")
    summarize.add_argument("paths", nargs="+")

    analyze = sub.add_parser("analyze", help="Aggregate JSONL runs into a site-ready results JSON.")
    analyze.add_argument("paths", nargs="+")
    analyze.add_argument("--out", default="docs/results.json")

    site = sub.add_parser("site", help="Alias for analyze with docs/results.json as the default output.")
    site.add_argument("paths", nargs="+")
    site.add_argument("--out", default="docs/results.json")

    judge = sub.add_parser("judge", help="Add Kimi judge annotations to run JSONL.")
    judge.add_argument("paths", nargs="+")
    judge.add_argument("--provider", choices=["rest", "cli", "sdk"], default="rest")
    judge.add_argument("--out", default="runs/judged.jsonl")
    judge.add_argument("--limit-steps", type=int, default=None)

    args = parser.parse_args(argv)
    client = OpenAICompatibleClient(args.base_url, args.api_key, args.timeout_s)

    try:
        if args.command == "models":
            for model in client.list_models():
                print(model)
            return 0
        if args.command == "doctor":
            return _doctor(args, client)
        if args.command == "build":
            corridor = _corridor_from_args(args)
            save_corridor(corridor, args.out)
            print(f"Wrote {Path(args.out).resolve()}")
            if args.zip_out:
                zip_path = export_nested_zip(corridor, args.zip_out)
                print(f"Wrote {zip_path}")
            return 0
        if args.command == "generate-bank":
            if args.provider == "sdk":
                bank = generate_lure_bank_sdk_sync(count=args.count, depth=args.depth, out_path=args.out)
            elif args.provider == "local":
                bank = generate_lure_bank_local(
                    client=client,
                    model=args.model,
                    count=args.count,
                    depth=args.depth,
                    out_path=args.out,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )
            else:
                bank = generate_lure_bank_template(count=args.count, depth=args.depth, out_path=args.out)
            print(f"Wrote {Path(args.out).resolve()} with {len(bank['arcs'])} arcs")
            return 0
        if args.command == "plan":
            plan_obj = generate_smt_plan(
                depth=args.depth,
                seed=args.seed,
                objective=args.objective,
                warmup=args.warmup,
                min_conflict_step=args.min_conflict_step,
            )
            save_plan(plan_obj, args.out)
            print(f"Wrote {Path(args.out).resolve()} with {len(plan_obj.capsules)} capsules")
            return 0
        if args.command == "surface-plan":
            plan_obj = load_plan(args.plan)
            if args.provider == "sdk":
                corridor = expand_plan_surface_sdk_sync(plan=plan_obj, out_path=args.out)
            elif args.provider == "local":
                corridor = expand_plan_surface_local(
                    client=client,
                    plan=plan_obj,
                    model=args.model,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    out_path=args.out,
                )
            else:
                corridor = corridor_from_plan(plan_obj)
                save_corridor(corridor, args.out)
            surface = corridor.metadata.get("surface", {})
            provider = surface.get("provider", "template")
            print(f"Wrote {Path(args.out).resolve()} with provider={provider}")
            return 0
        if args.command == "run":
            rows = _run(args, client)
            append_jsonl(args.out, rows)
            print(f"Wrote {Path(args.out).resolve()}")
            _print_run_summaries(rows)
            return 0
        if args.command == "probe":
            corridor = _corridor_from_args(args)
            config = RunConfig(
                model=args.model,
                defense=args.defense,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                no_think=args.no_think,
                extra_body=_extra_body_from_args(args),
            )
            row = run_probe_eval(
                client=client,
                corridor=corridor,
                config=config,
                trial_id=f"{args.model}:probe-seed{args.seed:04d}",
            )
            append_jsonl(args.out, [row])
            print(f"Wrote {Path(args.out).resolve()}")
            print(
                f"pre_stable={row['probe_delta']['pre_stable']} "
                f"post_stable={row['probe_delta']['post_stable']} "
                f"new_drift={row['probe_delta']['new_drift_fields']}"
            )
            return 0
        if args.command == "probe-plans":
            rows = _probe_plans(args, client, out_path=args.out)
            print(f"Wrote {Path(args.out).resolve()}")
            _print_probe_plan_summaries(rows)
            return 0
        if args.command == "queue-enqueue-plans":
            result = enqueue_planned_probe_jobs(
                client=client,
                queue_dir=args.queue_dir,
                models=resolve_models(args.models),
                seeds=parse_int_spec(args.seeds),
                objectives=parse_csv(args.objectives, OBJECTIVES),
                depth=args.depth,
                warmup=args.warmup,
                min_conflict_step=args.min_conflict_step,
                surface_provider=args.surface_provider,
                surface_model=args.surface_model,
                surface_temperature=args.surface_temperature,
                surface_max_tokens=args.surface_max_tokens,
                corpus_dir=args.corpus_dir,
                out_path=args.out,
                defense=args.defense,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                no_think=args.no_think,
                extra_body=_extra_body_from_args(args),
            )
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "queue-work":
            worker_id = args.worker_id or f"{Path.cwd().name}-{os.getpid()}"
            requested = resolve_models(args.models)
            available = set(client.list_models())
            runnable = [model for model in requested if model in available]
            result = run_worker(
                client=client,
                queue_dir=args.queue_dir,
                models=runnable,
                worker_id=worker_id,
                max_jobs=args.max_jobs,
                poll=args.poll,
                poll_interval_s=args.poll_interval_s,
            )
            print(json.dumps({"worker_id": worker_id, "models": runnable, **result}, indent=2))
            return 0
        if args.command == "queue-status":
            print(json.dumps(queue_status(args.queue_dir, stale_threshold_m=args.stale_threshold_m), indent=2))
            return 0
        if args.command == "queue-reclaim":
            result = reclaim_stale_jobs(args.queue_dir, stale_threshold_m=args.stale_threshold_m)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "benchmark":
            rows = _benchmark(args, client, out_path=args.out)
            print(f"Wrote {Path(args.out).resolve()}")
            _print_run_summaries([row for row in rows if row.get("steps")])
            missing = [row["model"] for row in rows if row.get("status") == "missing_model"]
            if missing:
                print(f"Missing models: {', '.join(missing)}")
            errors = [row for row in rows if row.get("status") == "error"]
            if errors:
                print(f"Errors: {len(errors)}")
            return 0
        if args.command == "search":
            runner = EvaluationRunner(client)
            rows = run_evolutionary_search(
                runner=runner,
                model=args.model,
                generations=args.generations,
                population=args.population,
                depth=args.depth,
                mode=args.mode,
                defense=args.defense,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                extra_body=_extra_body_from_args(args),
                no_think=args.no_think,
                rng_seed=args.seed,
            )
            append_jsonl(args.out, rows)
            print(f"Wrote {Path(args.out).resolve()}")
            _print_search_best(rows)
            return 0
        if args.command == "summarize":
            _summarize_files(args.paths)
            return 0
        if args.command in {"analyze", "site"}:
            analysis = write_analysis(args.paths, args.out)
            print(f"Wrote {Path(args.out).resolve()}")
            print(
                f"runs={analysis['runs']} groups={len(analysis['groups'])} "
                f"effects={len(analysis['effects'])} missing={analysis['missing_models']}"
            )
            return 0
        if args.command == "judge":
            judge_client = _judge_from_provider(args.provider)
            judged = judge_jsonl(
                inputs=args.paths,
                out_path=args.out,
                judge=judge_client,
                limit_steps=args.limit_steps,
            )
            print(f"Wrote {Path(args.out).resolve()} with {judged} judged steps")
            return 0
    except (LLMError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error("unreachable")
    return 2


def _add_corridor_generation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fact", default=None)
    parser.add_argument("--answers", default=None, help="Comma-separated password answers.")
    parser.add_argument("--difficulty", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--lure-strength", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--lure-mode", choices=["conflict", "decoy", "none", "progressive"], default="conflict")


def _corridor_from_args(args: argparse.Namespace):
    if getattr(args, "corridor", None):
        return load_corridor(args.corridor)
    if getattr(args, "plan", None):
        if getattr(args, "lure_bank", None):
            raise ValueError("--plan and --lure-bank both define lure surfaces; choose one")
        return corridor_from_plan(load_plan(args.plan))
    answers = None
    if args.answers:
        answers = [part.strip() for part in args.answers.split(",")]
    corridor = generate_corridor(
        depth=args.depth,
        seed=args.seed,
        fact=args.fact,
        answers=answers,
        difficulty=args.difficulty,
        lure_strength=args.lure_strength,
        lure_mode=args.lure_mode,
    )
    if getattr(args, "lure_bank", None):
        corridor = apply_lure_bank(corridor, load_lure_bank(args.lure_bank), getattr(args, "bank_arc", None))
    return corridor


def _run(args: argparse.Namespace, client: OpenAICompatibleClient) -> list[dict[str, Any]]:
    runner = EvaluationRunner(client)
    rows: list[dict[str, Any]] = []
    modes = ["cumulative", "reset"] if args.compare_reset else [args.mode]
    for trial in range(args.trials):
        if args.corridor:
            corridor = load_corridor(args.corridor)
        else:
            trial_args = argparse.Namespace(**vars(args))
            trial_args.seed = args.seed + trial
            corridor = _corridor_from_args(trial_args)
        for mode in modes:
            config = RunConfig(
                model=args.model,
                mode=mode,
                defense=args.defense,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                stop_on_failure=args.stop_on_failure,
                no_think=args.no_think,
                extra_body=_extra_body_from_args(args),
            )
            rows.append(
                runner.run_corridor(
                    corridor,
                    config,
                    trial_id=f"trial{trial:03d}-{mode}",
                )
            )
    return rows


def _benchmark(
    args: argparse.Namespace,
    client: OpenAICompatibleClient,
    *,
    out_path: str | None = None,
) -> list[dict[str, Any]]:
    runner = EvaluationRunner(client)
    requested_models = resolve_models(args.models)
    available_models = set(client.list_models())
    cases = build_cases(
        seeds=parse_int_spec(args.seeds),
        depths=parse_int_spec(args.depths),
        difficulties=parse_int_spec(args.difficulties),
        lure_strengths=parse_int_spec(args.lure_strengths),
        lure_modes=parse_csv(args.lure_modes, LURE_MODES),
        modes=parse_csv(args.modes, MODES),
        defenses=parse_csv(args.defenses, ["strict", "bare"]),
    )
    rows: list[dict[str, Any]] = []

    for model in requested_models:
        if model not in available_models:
            if not args.skip_missing:
                row = {
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "missing_model",
                    "model": model,
                    "available_models_checked": True,
                }
                rows.append(row)
                if out_path:
                    append_jsonl(out_path, [row])
            continue
        for case in cases:
            corridor = generate_corridor(
                depth=case.depth,
                seed=case.seed,
                difficulty=case.difficulty,
                lure_strength=case.lure_strength,
                lure_mode=case.lure_mode,
            )
            config = RunConfig(
                model=model,
                mode=case.mode,
                defense=case.defense,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                no_think=args.no_think,
                extra_body=_extra_body_from_args(args),
            )
            try:
                row = runner.run_corridor(corridor, config, trial_id=f"{model}:{case.case_id}")
            except LLMError as exc:
                row = {
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "error",
                    "model": model,
                    "case_id": case.case_id,
                    "error": str(exc),
                    "mode": case.mode,
                    "defense": case.defense,
                    "seed": case.seed,
                    "depth": case.depth,
                    "corridor_metadata": {
                        "difficulty": case.difficulty,
                        "lure_strength": case.lure_strength,
                        "lure_mode": case.lure_mode,
                    },
                }
            row["protocol_version"] = PROTOCOL_VERSION
            row["case_id"] = case.case_id
            rows.append(row)
            if out_path:
                append_jsonl(out_path, [row])
    return rows


def _probe_plans(
    args: argparse.Namespace,
    client: OpenAICompatibleClient,
    *,
    out_path: str,
) -> list[dict[str, Any]]:
    requested_models = resolve_models(args.models)
    available_models = set(client.list_models())
    seeds = parse_int_spec(args.seeds)
    objectives = parse_csv(args.objectives, OBJECTIVES)
    completed = _existing_trial_ids(out_path) if args.resume else set()
    corridors = []

    for seed in seeds:
        for objective in objectives:
            case_id = (
                f"planned-{objective}-seed{seed:04d}-d{args.depth}-"
                f"{args.surface_provider}-{args.defense}"
            )
            plan_obj, corridor = _load_or_generate_probe_plan_case(
                args=args,
                client=client,
                seed=seed,
                objective=objective,
                case_id=case_id,
            )
            if args.corpus_dir:
                corpus_dir = Path(args.corpus_dir)
                save_plan(plan_obj, corpus_dir / f"{case_id}.plan.json")
                save_corridor(corridor, corpus_dir / f"{case_id}.corridor.json")
            corridors.append((case_id, objective, plan_obj, corridor))

    rows: list[dict[str, Any]] = []
    for model in requested_models:
        if model not in available_models:
            if not args.skip_missing:
                row = {
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "missing_model",
                    "model": model,
                    "available_models_checked": True,
                }
                rows.append(row)
                append_jsonl(out_path, [row])
            continue

        for case_id, objective, plan_obj, corridor in corridors:
            trial_id = f"{model}:{case_id}"
            if trial_id in completed:
                continue
            config = RunConfig(
                model=model,
                defense=args.defense,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                no_think=args.no_think,
                extra_body=_extra_body_from_args(args),
            )
            try:
                row = run_probe_eval(
                    client=client,
                    corridor=corridor,
                    config=config,
                    trial_id=trial_id,
                )
                row["status"] = "ok"
            except LLMError as exc:
                row = {
                    "trial_id": trial_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "error",
                    "model": model,
                    "case_id": case_id,
                    "error": str(exc),
                    "seed": plan_obj.seed,
                    "depth": plan_obj.depth,
                    "objective": objective,
                }
            row["protocol_version"] = PROTOCOL_VERSION
            row["case_id"] = case_id
            row["objective"] = objective
            row["plan_id"] = plan_obj.plan_id
            row["surface_provider"] = args.surface_provider
            rows.append(row)
            append_jsonl(out_path, [row])
            _print_probe_plan_progress(row)
    return rows


def _load_or_generate_probe_plan_case(
    *,
    args: argparse.Namespace,
    client: OpenAICompatibleClient,
    seed: int,
    objective: str,
    case_id: str,
):
    plan_path: Path | None = None
    corridor_path: Path | None = None
    if args.corpus_dir:
        corpus_dir = Path(args.corpus_dir)
        plan_path = corpus_dir / f"{case_id}.plan.json"
        corridor_path = corpus_dir / f"{case_id}.corridor.json"
        if plan_path.exists() and corridor_path.exists():
            return load_plan(plan_path), load_corridor(corridor_path)

    if plan_path is not None and plan_path.exists():
        plan_obj = load_plan(plan_path)
    else:
        plan_obj = generate_smt_plan(
            depth=args.depth,
            seed=seed,
            objective=objective,
            warmup=args.warmup,
            min_conflict_step=args.min_conflict_step,
        )
        if plan_path is not None:
            save_plan(plan_obj, plan_path)

    corridor = _surface_plan_for_probe_matrix(
        args=args,
        client=client,
        plan_obj=plan_obj,
    )
    if corridor_path is not None:
        save_corridor(corridor, corridor_path)
    return plan_obj, corridor


def _surface_plan_for_probe_matrix(
    *,
    args: argparse.Namespace,
    client: OpenAICompatibleClient,
    plan_obj,
):
    if args.surface_provider == "sdk":
        return expand_plan_surface_sdk_sync(plan=plan_obj)
    if args.surface_provider == "local":
        return expand_plan_surface_local(
            client=client,
            plan=plan_obj,
            model=args.surface_model,
            max_tokens=args.surface_max_tokens,
            temperature=args.surface_temperature,
        )
    return corridor_from_plan(plan_obj)


def _existing_trial_ids(path: str | Path) -> set[str]:
    target = Path(path)
    if not target.exists():
        return set()
    trial_ids: set[str] = set()
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            trial_id = item.get("trial_id")
            if isinstance(trial_id, str):
                trial_ids.add(trial_id)
    return trial_ids


def _extra_body_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    raw = getattr(args, "extra_body_json", None)
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--extra-body-json must decode to a JSON object")
    return value


def _judge_from_provider(provider: str):
    if provider == "rest":
        return KimiJudge.from_env()
    if provider == "cli":
        return KimiJudge.from_cli()
    if provider == "sdk":
        return KimiJudge.from_sdk()
    raise ValueError(f"Unknown judge provider: {provider}")


def _doctor(args: argparse.Namespace, client: OpenAICompatibleClient) -> int:
    requested = resolve_models(args.models)
    available = set(client.list_models())
    present = [model for model in requested if model in available]
    missing = [model for model in requested if model not in available]
    print("Target model availability:")
    for model in requested:
        marker = "ok" if model in available else "missing"
        print(f"- {marker}: {model}")

    load_dotenv(args.dotenv)
    import os

    has_kimi_key = bool(os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"))
    print(f"Kimi judge key configured: {'yes' if has_kimi_key else 'no'}")
    if args.check_kimi:
        if not has_kimi_key:
            print("Kimi check skipped: no KIMI_API_KEY or MOONSHOT_API_KEY configured.")
        else:
            judge = KimiJudge.from_env(dotenv_path=args.dotenv)
            models = judge.client.list_models()
            print(f"Kimi endpoint reachable; {len(models)} models listed.")
    return 0 if present and not missing else 1


def _print_run_summaries(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        summary = row["summary"]
        print(
            f"{row['trial_id']} {row['mode']}/{row['defense']} "
            f"password_rate={summary['password_rate']:.2f} "
            f"invariant_rate={summary['invariant_rate']:.2f} "
            f"drift_rate={summary.get('drift_rate', summary['compromise_rate']):.2f} "
            f"contract_rate={summary.get('contract_rate', 0.0):.2f} "
            f"first_drift={summary.get('first_drift_step', summary['first_compromise_step'])}"
        )


def _print_search_best(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    best = max(rows, key=lambda row: row.get("search", {}).get("fitness", 0.0))
    print(
        "best "
        f"fitness={best['search']['fitness']:.3f} "
        f"candidate={best['search']['candidate']} "
        f"summary={best['summary']}"
    )


def _print_probe_plan_progress(row: dict[str, Any]) -> None:
    if row.get("status") == "error":
        print(f"{row['trial_id']} error={row['error']}")
        return
    if row.get("status") == "missing_model":
        print(f"{row['model']} missing")
        return
    delta = row.get("probe_delta", {})
    summary = row.get("summary", {})
    print(
        f"{row['trial_id']} "
        f"password_rate={summary.get('password_rate', 0.0):.2f} "
        f"contract_rate={summary.get('contract_rate', 0.0):.2f} "
        f"new_drift={delta.get('new_drift_fields', [])}"
    )


def _print_probe_plan_summaries(rows: list[dict[str, Any]]) -> None:
    completed = [row for row in rows if row.get("status") == "ok"]
    errors = [row for row in rows if row.get("status") == "error"]
    missing = [row for row in rows if row.get("status") == "missing_model"]
    print(f"completed={len(completed)} errors={len(errors)} missing={len(missing)}")


def _summarize_files(paths: list[str]) -> None:
    rows = load_jsonl(paths)
    if not rows:
        print("No rows.")
        return

    flat_steps = []
    for row in rows:
        flat_steps.extend(row.get("steps", []))
    summary = summarize_steps(flat_steps)
    missing_models = sorted({row["model"] for row in rows if row.get("status") == "missing_model"})
    print(json.dumps({"runs": len([row for row in rows if row.get("steps")]), "missing_models": missing_models, **summary}, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
