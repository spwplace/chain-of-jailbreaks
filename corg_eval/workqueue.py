from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .corridor import load_corridor, save_corridor
from .llm import LLMError, OpenAICompatibleClient
from .planner import load_plan, save_plan
from .probes import run_probe_eval
from .runner import RunConfig, append_jsonl
from .smt import generate_smt_plan
from .surface import expand_plan_surface_local, expand_plan_surface_sdk_sync


QUEUE_SCHEMA_VERSION = "coj-workqueue-v0.1"

_shutdown_requested = False


def _request_shutdown(signum: int, frame: Any) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def enqueue_planned_probe_jobs(
    *,
    client: OpenAICompatibleClient,
    queue_dir: str | Path,
    models: list[str],
    seeds: list[int],
    objectives: list[str],
    depth: int,
    warmup: int,
    min_conflict_step: int,
    surface_provider: str,
    surface_model: str,
    surface_temperature: float,
    surface_max_tokens: int,
    corpus_dir: str | Path,
    out_path: str | Path,
    defense: str,
    temperature: float,
    max_tokens: int,
    no_think: bool,
    extra_body: dict[str, Any] | None,
) -> dict[str, int]:
    dirs = ensure_queue_dirs(queue_dir)
    corpus = Path(corpus_dir)
    created = 0
    reused = 0
    skipped = 0

    for seed in seeds:
        for objective in objectives:
            case_id = f"planned-{objective}-seed{seed:04d}-d{depth}-{surface_provider}-{defense}"
            plan_path = corpus / f"{case_id}.plan.json"
            corridor_path = corpus / f"{case_id}.corridor.json"
            _ensure_planned_case(
                client=client,
                seed=seed,
                objective=objective,
                depth=depth,
                warmup=warmup,
                min_conflict_step=min_conflict_step,
                surface_provider=surface_provider,
                surface_model=surface_model,
                surface_temperature=surface_temperature,
                surface_max_tokens=surface_max_tokens,
                plan_path=plan_path,
                corridor_path=corridor_path,
            )

            for model in models:
                trial_id = f"{model}:{case_id}"
                job = {
                    "schema_version": QUEUE_SCHEMA_VERSION,
                    "kind": "planned_probe",
                    "job_id": job_id_for(trial_id),
                    "trial_id": trial_id,
                    "model": model,
                    "case_id": case_id,
                    "objective": objective,
                    "seed": seed,
                    "depth": depth,
                    "plan_path": str(plan_path),
                    "corridor_path": str(corridor_path),
                    "out_path": str(out_path),
                    "config": {
                        "defense": defense,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "no_think": no_think,
                        "extra_body": extra_body,
                    },
                    "surface": {
                        "provider": surface_provider,
                        "model": surface_model,
                    },
                    "created_at": now_iso(),
                }
                job_path = dirs["pending"] / f"{job['job_id']}.json"
                if _job_exists(dirs, str(job["job_id"])):
                    reused += 1
                    continue
                if trial_id in existing_trial_ids(out_path):
                    skipped += 1
                    done_path = dirs["done"] / f"{job['job_id']}.json"
                    write_json_atomic(done_path, {**job, "status": "already_completed"})
                    continue
                write_json_atomic(job_path, job)
                created += 1
    return {"created": created, "reused": reused, "skipped_completed": skipped}


def run_worker(
    *,
    client: OpenAICompatibleClient,
    queue_dir: str | Path,
    models: list[str],
    worker_id: str,
    max_jobs: int,
    poll: bool,
    poll_interval_s: float,
) -> dict[str, int]:
    import signal

    global _shutdown_requested
    _shutdown_requested = False
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    dirs = ensure_queue_dirs(queue_dir)
    completed = 0
    failed = 0
    skipped = 0

    while True:
        if _shutdown_requested:
            break
        claimed = claim_next_job(dirs, models=models, worker_id=worker_id)
        if claimed is None:
            if poll and (max_jobs <= 0 or completed + failed < max_jobs):
                time.sleep(poll_interval_s)
                continue
            break
        running_path, job = claimed
        try:
            if job.get("trial_id") in existing_trial_ids(job["out_path"]):
                skipped += 1
                _finish_job(dirs, running_path, job, status="already_completed")
            else:
                row = execute_job(client=client, job=job)
                append_jsonl(job["out_path"], [row])
                completed += 1
                _finish_job(dirs, running_path, {**job, "row_status": row.get("status", "ok")}, status="done")
        except (LLMError, OSError, RuntimeError, ValueError) as exc:
            failed += 1
            _finish_job(dirs, running_path, {**job, "error": str(exc)}, status="failed")
        if max_jobs > 0 and completed + failed >= max_jobs:
            break
    return {"completed": completed, "failed": failed, "skipped_completed": skipped}


def queue_status(queue_dir: str | Path, stale_threshold_m: int = 60) -> dict[str, Any]:
    dirs = ensure_queue_dirs(queue_dir)
    counts = {name: len(list(path.glob("*.json"))) for name, path in dirs.items()}
    pending_models: dict[str, int] = {}
    for path in dirs["pending"].glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        model = str(job.get("model", "unknown"))
        pending_models[model] = pending_models.get(model, 0) + 1

    stale = _find_stale_jobs(dirs, stale_threshold_m)
    return {
        "queue_dir": str(Path(queue_dir)),
        "counts": counts,
        "pending_models": pending_models,
        "stale_threshold_m": stale_threshold_m,
        "stale_jobs": len(stale),
        "stale": stale,
    }


def reclaim_stale_jobs(queue_dir: str | Path, stale_threshold_m: int = 60) -> dict[str, int]:
    dirs = ensure_queue_dirs(queue_dir)
    stale = _find_stale_jobs(dirs, stale_threshold_m)
    reclaimed = 0
    for info in stale:
        running_path = info["path"]
        try:
            job = json.loads(running_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        job["reclaimed_at"] = now_iso()
        job["previous_claimed_by"] = job.get("claimed_by")
        job["previous_claimed_at"] = job.get("claimed_at")
        for key in ("claimed_by", "claimed_at", "host", "pid"):
            job.pop(key, None)
        pending_path = dirs["pending"] / f"{job['job_id']}.json"
        write_json_atomic(pending_path, job)
        try:
            running_path.unlink()
        except FileNotFoundError:
            continue
        reclaimed += 1
    return {"reclaimed": reclaimed, "stale_found": len(stale)}


def _find_stale_jobs(dirs: dict[str, Path], stale_threshold_m: int) -> list[dict[str, Any]]:
    cutoff = time.time() - stale_threshold_m * 60
    stale: list[dict[str, Any]] = []
    for path in dirs["running"].glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        claimed_at = job.get("claimed_at")
        if not claimed_at:
            stale.append({"job_id": job.get("job_id", path.stem), "path": path, "reason": "no_claimed_at"})
            continue
        try:
            ts = datetime.fromisoformat(claimed_at).timestamp()
        except (ValueError, TypeError):
            stale.append({"job_id": job.get("job_id", path.stem), "path": path, "reason": "bad_claimed_at"})
            continue
        if ts < cutoff:
            stale.append({"job_id": job.get("job_id", path.stem), "path": path, "reason": "old", "claimed_at": claimed_at})
    return stale


def execute_job(*, client: OpenAICompatibleClient, job: dict[str, Any]) -> dict[str, Any]:
    if job.get("kind") != "planned_probe":
        raise ValueError(f"Unsupported job kind: {job.get('kind')!r}")
    config_data = dict(job.get("config", {}))
    config = RunConfig(
        model=str(job["model"]),
        defense=str(config_data.get("defense", "strict")),
        temperature=float(config_data.get("temperature", 0.0)),
        max_tokens=int(config_data.get("max_tokens", 8192)),
        no_think=bool(config_data.get("no_think", False)),
        extra_body=config_data.get("extra_body"),
    )
    row = run_probe_eval(
        client=client,
        corridor=load_corridor(job["corridor_path"]),
        config=config,
        trial_id=str(job["trial_id"]),
    )
    row["protocol_version"] = "coj-protocol-v0.2"
    row["status"] = "ok"
    row["case_id"] = job.get("case_id")
    row["objective"] = job.get("objective")
    row["plan_path"] = job.get("plan_path")
    row["surface_provider"] = job.get("surface", {}).get("provider")
    row["worker"] = job.get("claimed_by")
    return row


def claim_next_job(
    dirs: dict[str, Path],
    *,
    models: list[str],
    worker_id: str,
) -> tuple[Path, dict[str, Any]] | None:
    allowed = set(models)
    for path in sorted(dirs["pending"].glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        if allowed and job.get("model") not in allowed:
            continue
        claimed = {
            **job,
            "claimed_by": worker_id,
            "claimed_at": now_iso(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }
        job_id = str(job.get("job_id", path.stem))
        running_path = dirs["running"] / f"{job_id}.json"
        # Atomically create the running file. If it already exists, another worker won.
        try:
            with open(running_path, "x", encoding="utf-8") as handle:
                handle.write(json.dumps(claimed, indent=2, ensure_ascii=True) + "\n")
        except FileExistsError:
            continue
        # Remove the pending file. If it's already gone, that's fine.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return running_path, claimed
    return None


def ensure_queue_dirs(queue_dir: str | Path) -> dict[str, Path]:
    root = Path(queue_dir)
    dirs = {
        "pending": root / "pending",
        "running": root / "running",
        "done": root / "done",
        "failed": root / "failed",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def existing_trial_ids(path: str | Path) -> set[str]:
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


def job_id_for(trial_id: str) -> str:
    digest = hashlib.sha1(trial_id.encode("utf-8")).hexdigest()[:16]
    safe = "".join(ch if ch.isalnum() else "-" for ch in trial_id.lower())
    return f"{safe[:96]}-{digest}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_planned_case(
    *,
    client: OpenAICompatibleClient,
    seed: int,
    objective: str,
    depth: int,
    warmup: int,
    min_conflict_step: int,
    surface_provider: str,
    surface_model: str,
    surface_temperature: float,
    surface_max_tokens: int,
    plan_path: Path,
    corridor_path: Path,
) -> None:
    if plan_path.exists() and corridor_path.exists():
        return
    if plan_path.exists():
        plan = load_plan(plan_path)
    else:
        plan = generate_smt_plan(
            depth=depth,
            seed=seed,
            objective=objective,
            warmup=warmup,
            min_conflict_step=min_conflict_step,
        )
        save_plan(plan, plan_path)

    if corridor_path.exists():
        return
    if surface_provider == "sdk":
        corridor = expand_plan_surface_sdk_sync(plan=plan)
    elif surface_provider == "local":
        corridor = expand_plan_surface_local(
            client=client,
            plan=plan,
            model=surface_model,
            max_tokens=surface_max_tokens,
            temperature=surface_temperature,
        )
    else:
        from .planner import corridor_from_plan

        corridor = corridor_from_plan(plan)
    save_corridor(corridor, corridor_path)


def _job_exists(dirs: dict[str, Path], job_id: str) -> bool:
    return any(path.exists() for path in _job_paths(dirs, job_id))


def _job_paths(dirs: dict[str, Path], job_id: str) -> list[Path]:
    paths = []
    for path in dirs.values():
        paths.extend(path.glob(f"{job_id}*.json"))
    return paths


def _finish_job(
    dirs: dict[str, Path],
    running_path: Path,
    job: dict[str, Any],
    *,
    status: str,
) -> None:
    payload = {**job, "finished_at": now_iso(), "status": status}
    target_dir = dirs["failed"] if status == "failed" else dirs["done"]
    target = target_dir / f"{job['job_id']}.json"
    write_json_atomic(running_path, payload)
    running_path.replace(target)
