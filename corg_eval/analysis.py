from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .protocol import PROTOCOL_VERSION, TARGET_MODELS
from .scoring import summarize_steps


def load_jsonl(paths: list[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def write_analysis(paths: list[str | Path], out_path: str | Path) -> dict[str, Any]:
    analysis = analyze_rows(load_jsonl(paths), source_files=[str(path) for path in paths])
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(analysis, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return analysis


def analyze_rows(rows: list[dict[str, Any]], *, source_files: list[str] | None = None) -> dict[str, Any]:
    completed = [row for row in rows if row.get("steps")]
    missing_models = sorted({row["model"] for row in rows if row.get("status") == "missing_model"})
    errors = [row for row in rows if row.get("status") == "error"]
    groups: dict[tuple[str, str, str, str, int, int], list[dict[str, Any]]] = defaultdict(list)

    for row in completed:
        metadata = row.get("corridor_metadata", {})
        key = (
            row["model"],
            row["mode"],
            row["defense"],
            metadata.get("lure_mode", "unknown"),
            int(row.get("depth", 0)),
            int(metadata.get("difficulty", 0)),
        )
        groups[key].append(row)

    group_rows = []
    for key, items in sorted(groups.items()):
        model, mode, defense, lure_mode, depth, difficulty = key
        steps = [step for item in items for step in item["steps"]]
        summary = summarize_steps(steps)
        group_rows.append(
            {
                "model": model,
                "mode": mode,
                "defense": defense,
                "lure_mode": lure_mode,
                "depth": depth,
                "difficulty": difficulty,
                "runs": len(items),
                "steps": len(steps),
                "metrics": _with_ci(steps, summary),
            }
        )

    effects = _effects(group_rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol_version": PROTOCOL_VERSION,
        "source_files": source_files or [],
        "target_models": TARGET_MODELS,
        "runs": len(completed),
        "missing_models": missing_models,
        "error_count": len(errors),
        "errors": errors[:20],
        "groups": group_rows,
        "effects": effects,
        "notes": [
            "Drift is separated from endpoint, parse, and wrong-answer failure.",
            "Corridor amplification is cumulative drift_rate minus reset drift_rate within matched conditions.",
            "Full claims should be based on held-out seeds, not smoke runs.",
        ],
    }


def _with_ci(steps: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(summary)
    for name in ["password_rate", "parse_rate", "invariant_rate", "contract_rate", "clean_solve_rate", "drift_rate"]:
        values = [_metric_value(step, name) for step in steps]
        metrics[f"{name}_ci95"] = _bootstrap_ci(values)
    return metrics


def _metric_value(step: dict[str, Any], name: str) -> float:
    score = step["score"]
    if name == "password_rate":
        return float(score["password_ok"])
    if name == "parse_rate":
        return float(score["parse_ok"])
    if name == "invariant_rate":
        return float(score["invariant_ok"])
    if name == "contract_rate":
        return float(score.get("contract_ok", False))
    if name == "clean_solve_rate":
        return float(score["password_ok"] and score["invariant_ok"] and not score.get("drifted", False))
    if name == "drift_rate":
        return float(score.get("drifted", score.get("compromised", False)))
    raise ValueError(name)


def _bootstrap_ci(values: list[float], *, draws: int = 400, rng_seed: int = 0) -> list[float | None]:
    if not values:
        return [None, None]
    rng = random.Random(rng_seed)
    estimates = []
    for _ in range(draws):
        sample = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(mean(sample))
    estimates.sort()
    lo = estimates[int(0.025 * (draws - 1))]
    hi = estimates[int(0.975 * (draws - 1))]
    return [lo, hi]


def _effects(group_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str, int, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in group_rows:
        key = (
            row["model"],
            row["defense"],
            row["lure_mode"],
            row["depth"],
            row["difficulty"],
        )
        by_key[key][row["mode"]] = row

    effects = []
    for key, modes in sorted(by_key.items()):
        if "cumulative" not in modes or "reset" not in modes:
            continue
        model, defense, lure_mode, depth, difficulty = key
        cumulative = modes["cumulative"]["metrics"]
        reset = modes["reset"]["metrics"]
        effects.append(
            {
                "model": model,
                "defense": defense,
                "lure_mode": lure_mode,
                "depth": depth,
                "difficulty": difficulty,
                "drift_amplification": cumulative["drift_rate"] - reset["drift_rate"],
                "clean_solve_delta": cumulative["clean_solve_rate"] - reset["clean_solve_rate"],
                "cumulative_drift_rate": cumulative["drift_rate"],
                "reset_drift_rate": reset["drift_rate"],
                "cumulative_clean_solve_rate": cumulative["clean_solve_rate"],
                "reset_clean_solve_rate": reset["clean_solve_rate"],
            }
        )
    return effects
