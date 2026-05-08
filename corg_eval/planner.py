from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .corridor import Capsule, Corridor, INVARIANTS, make_riddle_variant, normalize_answer
from .tactics import TACTICS, markers_for, stable_value
from .template_render import render_lure_template


PLAN_SCHEMA_VERSION = "corridor-plan-v0.1"


@dataclass(frozen=True)
class CapsulePlan:
    index: int
    answer: str
    clues: list[str]
    riddle_variant: str
    template_id: str
    tactic: str
    target_field: str
    stable_value: str
    claim_value: str
    markers: list[str]
    intensity: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CorridorPlan:
    schema_version: str
    plan_id: str
    seed: int
    objective: str
    depth: int
    fact: str
    capsules: list[CapsulePlan]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CorridorPlan":
        if data.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise ValueError(f"Unsupported plan schema: {data.get('schema_version')!r}")
        capsules = [CapsulePlan(**item) for item in data["capsules"]]
        return cls(
            schema_version=data["schema_version"],
            plan_id=data["plan_id"],
            seed=int(data["seed"]),
            objective=str(data["objective"]),
            depth=int(data["depth"]),
            fact=str(data["fact"]),
            capsules=capsules,
            metadata=dict(data.get("metadata", {})),
        )


def save_plan(plan: CorridorPlan, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def load_plan(path: str | Path) -> CorridorPlan:
    return CorridorPlan.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def corridor_from_plan(plan: CorridorPlan) -> Corridor:
    capsules: list[Capsule] = []
    for capsule_plan in plan.capsules:
        rng = random.Random(plan.seed * 10_000 + capsule_plan.index)
        riddle = make_riddle_variant(
            capsule_plan.answer,
            capsule_plan.clues,
            capsule_plan.riddle_variant,
            rng=rng,
        )
        lure = _render_capsule_lure(plan, capsule_plan)
        capsules.append(
            Capsule(
                index=capsule_plan.index,
                answer=normalize_answer(capsule_plan.answer),
                riddle=riddle,
                lure=lure,
                drift_markers=capsule_plan.markers,
                metadata={
                    **capsule_plan.metadata,
                    "lure": capsule_plan.tactic,
                    "lure_mode": "planned",
                    "template_id": capsule_plan.template_id,
                    "target_field": capsule_plan.target_field,
                    "claim_value": capsule_plan.claim_value,
                    "plan_id": plan.plan_id,
                    "objective": plan.objective,
                },
            )
        )
    return Corridor(
        id=f"planned-{plan.plan_id}",
        seed=plan.seed,
        fact=plan.fact,
        invariants=dict(INVARIANTS),
        capsules=capsules,
        metadata={
            **plan.metadata,
            "lure_mode": "planned",
            "plan_id": plan.plan_id,
            "objective": plan.objective,
            "schema_version": plan.schema_version,
        },
    )


def _render_capsule_lure(plan: CorridorPlan, capsule_plan: CapsulePlan) -> str:
    tactic = TACTICS[capsule_plan.tactic]
    context = {
        "plan_id": plan.plan_id,
        "objective": plan.objective,
        "capsule_index": capsule_plan.index,
        "depth": plan.depth,
        "tactic_id": capsule_plan.tactic,
        "tactic_label": tactic.label,
        "tactic_description": tactic.description,
        "target_field": capsule_plan.target_field,
        "stable_value": capsule_plan.stable_value,
        "claim_value": capsule_plan.claim_value,
        "intensity": capsule_plan.intensity,
        "intensity_word": _intensity_word(capsule_plan.intensity),
        "previous_index": max(1, capsule_plan.index - 1),
        "is_decoy": capsule_plan.tactic == "decoy",
    }
    return render_lure_template(capsule_plan.template_id, context)


def _intensity_word(intensity: int) -> str:
    if intensity <= 1:
        return "lightly"
    if intensity == 2:
        return "clearly"
    return "strongly"


def validate_plan(plan: CorridorPlan) -> None:
    if plan.depth != len(plan.capsules):
        raise ValueError("plan depth does not match capsule count")
    seen_indexes = set()
    for capsule in plan.capsules:
        if capsule.index in seen_indexes:
            raise ValueError(f"duplicate capsule index: {capsule.index}")
        seen_indexes.add(capsule.index)
        if capsule.tactic not in TACTICS:
            raise ValueError(f"unknown tactic: {capsule.tactic}")
        if capsule.target_field not in TACTICS[capsule.tactic].allowed_fields:
            raise ValueError(
                f"tactic {capsule.tactic!r} does not allow target field {capsule.target_field!r}"
            )
        if capsule.stable_value != stable_value(capsule.target_field):
            raise ValueError(f"stable value mismatch for {capsule.target_field}")
        expected_markers = markers_for(
            capsule.target_field,
            capsule.claim_value,
            include=TACTICS[capsule.tactic].drift_capable,
        )
        if capsule.markers != expected_markers:
            raise ValueError(f"marker mismatch in capsule {capsule.index}")
