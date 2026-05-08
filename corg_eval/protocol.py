from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


PROTOCOL_VERSION = "coj-protocol-v0.2"

TARGET_MODELS = [
    "nvidia/nemotron-3-nano-omni",
    "qwen/qwen3.6-35b-a3b",
    "qwen/qwen3.6-27b",
    "google/gemma-4-31b",
    "google/gemma-4-e2b",
    "google/gemma-4-e4b",
    "ternary-bonsai-8b-mlx",
]

LURE_MODES = ["none", "decoy", "conflict", "progressive"]
MODES = ["reset", "cumulative"]


@dataclass(frozen=True)
class BenchmarkCase:
    seed: int
    depth: int
    difficulty: int
    lure_strength: int
    lure_mode: str
    mode: str
    defense: str

    @property
    def case_id(self) -> str:
        return (
            f"seed{self.seed:04d}-d{self.depth}-k{self.difficulty}-"
            f"l{self.lure_strength}-{self.lure_mode}-{self.mode}-{self.defense}"
        )


def parse_int_spec(spec: str) -> list[int]:
    """Parse comma values and half-open ranges like 0:10 or 0:10:2."""

    values: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            bits = [int(bit) for bit in part.split(":")]
            if len(bits) == 2:
                start, stop = bits
                step = 1
            elif len(bits) == 3:
                start, stop, step = bits
            else:
                raise ValueError(f"Invalid integer range: {part}")
            values.extend(range(start, stop, step))
        else:
            values.append(int(part))
    if not values:
        raise ValueError(f"No integers parsed from {spec!r}")
    return values


def parse_csv(spec: str, allowed: Iterable[str] | None = None) -> list[str]:
    values = [part.strip() for part in spec.split(",") if part.strip()]
    if not values:
        raise ValueError(f"No values parsed from {spec!r}")
    if allowed is not None:
        allowed_set = set(allowed)
        unknown = [value for value in values if value not in allowed_set]
        if unknown:
            raise ValueError(f"Unknown values {unknown}; allowed values are {sorted(allowed_set)}")
    return values


def resolve_models(selector: str) -> list[str]:
    if selector == "target":
        return list(TARGET_MODELS)
    return parse_csv(selector)


def build_cases(
    *,
    seeds: list[int],
    depths: list[int],
    difficulties: list[int],
    lure_strengths: list[int],
    lure_modes: list[str],
    modes: list[str],
    defenses: list[str],
) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for seed in seeds:
        for depth in depths:
            for difficulty in difficulties:
                for lure_strength in lure_strengths:
                    for lure_mode in lure_modes:
                        for mode in modes:
                            for defense in defenses:
                                cases.append(
                                    BenchmarkCase(
                                        seed=seed,
                                        depth=depth,
                                        difficulty=difficulty,
                                        lure_strength=lure_strength,
                                        lure_mode=lure_mode,
                                        mode=mode,
                                        defense=defense,
                                    )
                                )
    return cases
