from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .corridor import Capsule, Corridor


BANK_SCHEMA_VERSION = "lure-bank-v0.1"


def load_lure_bank(path: str | Path) -> dict[str, Any]:
    bank = json.loads(Path(path).read_text(encoding="utf-8"))
    if bank.get("schema_version") != BANK_SCHEMA_VERSION:
        raise ValueError(f"Unsupported lure bank schema: {bank.get('schema_version')!r}")
    if not isinstance(bank.get("arcs"), list) or not bank["arcs"]:
        raise ValueError("Lure bank must contain at least one arc.")
    return bank


def save_lure_bank(bank: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bank, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def apply_lure_bank(corridor: Corridor, bank: dict[str, Any], arc_id: str | None = None) -> Corridor:
    arc = select_arc(bank, arc_id)
    lures = arc.get("lures", [])
    if not isinstance(lures, list) or not lures:
        raise ValueError(f"Arc {arc.get('arc_id')!r} has no lures.")

    capsules: list[Capsule] = []
    for index, capsule in enumerate(corridor.capsules):
        lure = lures[index % len(lures)]
        capsules.append(
            replace(
                capsule,
                lure=str(lure["text"]),
                drift_markers=[str(marker) for marker in lure.get("markers", [])],
                metadata={
                    **capsule.metadata,
                    "lure": str(lure.get("name", f"bank_lure_{index + 1}")),
                    "lure_mode": "bank",
                    "bank_id": bank.get("bank_id"),
                    "arc_id": arc.get("arc_id"),
                    "target_drift": arc.get("target_drift", []),
                },
            )
        )

    metadata = {
        **corridor.metadata,
        "lure_mode": "bank",
        "bank_id": bank.get("bank_id"),
        "arc_id": arc.get("arc_id"),
        "arc_description": arc.get("description", ""),
        "generator": bank.get("generator", {}),
    }
    return replace(
        corridor,
        id=f"{corridor.id}-bank-{arc.get('arc_id', 'arc')}",
        capsules=capsules,
        metadata=metadata,
    )


def select_arc(bank: dict[str, Any], arc_id: str | None = None) -> dict[str, Any]:
    arcs = bank["arcs"]
    if arc_id is None:
        return arcs[0]
    for arc in arcs:
        if arc.get("arc_id") == arc_id:
            return arc
    raise ValueError(f"Arc {arc_id!r} not found in bank {bank.get('bank_id')!r}.")

