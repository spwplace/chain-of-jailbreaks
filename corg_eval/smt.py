from __future__ import annotations

import random
from typing import Any

from .corridor import ANSWER_BANK, FACTS, normalize_answer
from .planner import PLAN_SCHEMA_VERSION, CapsulePlan, CorridorPlan, validate_plan
from .tactics import (
    RIDDLE_VARIANTS,
    TACTICS,
    TEMPLATE_IDS,
    claim_value,
    markers_for,
    objective_fields,
    stable_value,
    tactic_ids_for_objective,
)


def generate_smt_plan(
    *,
    depth: int,
    seed: int,
    objective: str = "mixed",
    warmup: int = 1,
    min_conflict_step: int = 2,
) -> CorridorPlan:
    """Generate a constrained corridor plan.

    The solver chooses a symbolic skeleton: answers, tactic families, target
    fields, templates, and riddle variants. Rendering remains deterministic and
    auditable, while generation can enforce matched structural constraints.
    """

    if depth < 1:
        raise ValueError("depth must be at least 1")
    if warmup < 0:
        raise ValueError("warmup must be non-negative")
    try:
        import z3
    except ImportError as exc:
        raise RuntimeError("Install generator extras with `pip install -e '.[generator]'`.") from exc

    rng = random.Random(seed)
    answer_items = ANSWER_BANK[:]
    rng.shuffle(answer_items)
    facts = FACTS[:]
    rng.shuffle(facts)
    tactic_ids = list(tactic_ids_for_objective(objective))
    rng.shuffle(tactic_ids)
    fields = list(objective_fields(objective))
    rng.shuffle(fields)
    template_ids = list(TEMPLATE_IDS)
    rng.shuffle(template_ids)
    riddle_variants = list(RIDDLE_VARIANTS)
    rng.shuffle(riddle_variants)

    answer_vars = [z3.Int(f"answer_{i}") for i in range(depth)]
    tactic_vars = [z3.Int(f"tactic_{i}") for i in range(depth)]
    field_vars = [z3.Int(f"field_{i}") for i in range(depth)]
    template_vars = [z3.Int(f"template_{i}") for i in range(depth)]
    riddle_vars = [z3.Int(f"riddle_{i}") for i in range(depth)]

    solver = z3.Solver()
    solver.set("random_seed", seed)

    for i in range(depth):
        solver.add(answer_vars[i] >= 0, answer_vars[i] < len(answer_items))
        solver.add(tactic_vars[i] >= 0, tactic_vars[i] < len(tactic_ids))
        solver.add(field_vars[i] >= 0, field_vars[i] < len(fields))
        solver.add(template_vars[i] >= 0, template_vars[i] < len(template_ids))
        solver.add(riddle_vars[i] >= 0, riddle_vars[i] < len(riddle_variants))
        solver.add(_allowed_tactic_field(z3, tactic_vars[i], field_vars[i], tactic_ids, fields))

        if i < min(warmup, depth):
            solver.add(tactic_vars[i] == tactic_ids.index("decoy"))
        if i + 1 < min_conflict_step:
            for tactic_id in ("belief_substitution", "authority_boundary", "format_pressure"):
                if tactic_id in tactic_ids:
                    solver.add(tactic_vars[i] != tactic_ids.index(tactic_id))
        if i > 0:
            solver.add(tactic_vars[i] != tactic_vars[i - 1])
            solver.add(field_vars[i] != field_vars[i - 1])
            solver.add(template_vars[i] != template_vars[i - 1])
            solver.add(riddle_vars[i] != riddle_vars[i - 1])

    if depth <= len(answer_items):
        solver.add(z3.Distinct(answer_vars))
    elif depth > 1:
        for i in range(1, depth):
            solver.add(answer_vars[i] != answer_vars[i - 1])

    drift_tactic_indexes = [
        index for index, tactic_id in enumerate(tactic_ids) if TACTICS[tactic_id].drift_capable
    ]

    required_fields = fields[: min(max(0, depth - min(warmup, depth)), len(fields))]
    for field in required_fields:
        field_index = fields.index(field)
        solver.add(
            z3.Or(
                [
                    z3.And(
                        field_vars[i] == field_index,
                        z3.Or([tactic_vars[i] == tactic_index for tactic_index in drift_tactic_indexes]),
                    )
                    for i in range(depth)
                ]
            )
        )

    if depth >= len(template_ids):
        for template_index in range(len(template_ids)):
            solver.add(z3.Or([template_var == template_index for template_var in template_vars]))

    for riddle_index in range(min(depth, 3)):
        solver.add(z3.Or([riddle_var == riddle_index for riddle_var in riddle_vars]))

    if depth >= 3 and "decoy" in tactic_ids:
        decoy_index = tactic_ids.index("decoy")
        solver.add(z3.Or([tactic_var == decoy_index for tactic_var in tactic_vars]))

    if drift_tactic_indexes and depth > warmup:
        solver.add(z3.Or([z3.Or([tactic_var == index for index in drift_tactic_indexes]) for tactic_var in tactic_vars]))

    if solver.check() != z3.sat:
        raise RuntimeError("SMT planner could not satisfy corridor constraints.")

    model = solver.model()
    capsules: list[CapsulePlan] = []
    for i in range(depth):
        answer_item = answer_items[model[answer_vars[i]].as_long()]
        tactic_id = tactic_ids[model[tactic_vars[i]].as_long()]
        target_field = fields[model[field_vars[i]].as_long()]
        value = claim_value(target_field, seed + i)
        drift_capable = TACTICS[tactic_id].drift_capable
        capsules.append(
            CapsulePlan(
                index=i + 1,
                answer=normalize_answer(str(answer_item["answer"])),
                clues=[str(clue) for clue in answer_item["clues"]],
                riddle_variant=riddle_variants[model[riddle_vars[i]].as_long()],
                template_id=template_ids[model[template_vars[i]].as_long()],
                tactic=tactic_id,
                target_field=target_field,
                stable_value=stable_value(target_field),
                claim_value=value,
                markers=markers_for(target_field, value, include=drift_capable),
                intensity=1 + ((seed + i) % 3),
                metadata={"answer_bank_index": ANSWER_BANK.index(answer_item)},
            )
        )

    plan = CorridorPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=f"smt-{objective}-{seed}-d{depth}",
        seed=seed,
        objective=objective,
        depth=depth,
        fact=facts[0],
        capsules=capsules,
        metadata={
            "planner": "z3",
            "warmup": warmup,
            "min_conflict_step": min_conflict_step,
            "objective_fields": list(objective_fields(objective)),
            "required_drift_fields": required_fields,
            "constraints": [
                "valid tactic/field pairs",
                "unique answers when depth permits",
                "required drift-capable objective field coverage",
                "no repeated adjacent tactic",
                "no repeated adjacent target field",
                "no repeated adjacent template",
                "no repeated adjacent riddle variant",
                "warmup decoy capsules",
            ],
        },
    )
    validate_plan(plan)
    return plan


def _allowed_tactic_field(
    z3: Any,
    tactic_var: Any,
    field_var: Any,
    tactic_ids: list[str],
    fields: list[str],
) -> Any:
    clauses = []
    for tactic_index, tactic_id in enumerate(tactic_ids):
        allowed = set(TACTICS[tactic_id].allowed_fields)
        for field_index, field in enumerate(fields):
            if field in allowed:
                clauses.append(z3.And(tactic_var == tactic_index, field_var == field_index))
    if not clauses:
        raise ValueError("No valid tactic/field combinations for objective.")
    return z3.Or(clauses)
