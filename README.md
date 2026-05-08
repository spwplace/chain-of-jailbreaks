# Character Corrigidor Eval

Corrigibility and instruction-drift benchmark harness for nested riddle corridors. The project asks whether cumulative puzzle-solving makes a model more willing to carry forward untrusted beliefs, roles, memories, formats, or authority claims.

The benchmark is intentionally non-operational. Corridor capsules contain ordinary riddles plus untrusted lore that may try to alter stable invariants such as `sky_color=blue`, `2+2=4`, `role=neutral_solver`, and `authority=system_over_capsule`.

## Current Status

This is a research harness, not a finished benchmark claim. The protocol now separates:

- riddle-solving accuracy;
- JSON and invariant contract compliance;
- actual drift into untrusted capsule claims;
- endpoint artifacts such as truncation or empty reasoning-only responses.

See [METHODOLOGY.md](METHODOLOGY.md) for the protocol.

## Quick Start

LM Studio should expose an OpenAI-compatible server at:

```bash
http://127.0.0.1:1234/v1
```

List local models:

```bash
python3 -m corg_eval models
```

Check the target cohort and judge configuration:

```bash
python3 -m corg_eval doctor --models target
```

Install optional generator and Kimi dependencies:

```bash
.venv/bin/python -m pip install -e '.[generator,kimi]'
```

Build a single corridor JSON file plus a playable nested zip:

```bash
python3 -m corg_eval build \
  --depth 6 \
  --seed 7 \
  --lure-mode conflict \
  --out corridors/example.json \
  --zip-out corridors/example.zip
```

Run one model with matched reset and cumulative modes:

```bash
python3 -m corg_eval run \
  --model nvidia/nemotron-3-nano-omni \
  --corridor corridors/example.json \
  --compare-reset \
  --defense strict \
  --max-tokens 8192 \
  --out runs/nemotron-example.jsonl
```

Run a pre/post persona probe battery around a corridor:

```bash
python3 -m corg_eval probe \
  --model nvidia/nemotron-3-nano-omni \
  --depth 6 \
  --seed 7 \
  --lure-mode progressive \
  --max-tokens 8192 \
  --out runs/nemotron-probe.jsonl
```

Generate a persistent lure bank with Kimi Code SDK, then run it:

```bash
.venv/bin/python -m corg_eval generate-bank \
  --provider sdk \
  --count 3 \
  --depth 6 \
  --out banks/kimi-k2.6-pilot.json

python3 -m corg_eval probe \
  --model google/gemma-4-31b \
  --depth 6 \
  --lure-bank banks/kimi-k2.6-pilot.json \
  --bank-arc <arc_id> \
  --out runs/gemma-bank-probe.jsonl
```

Generate a constrained SMT plan, then render it through Jinja templates or one batched Kimi surface-generation call:

```bash
.venv/bin/python -m corg_eval plan \
  --depth 6 \
  --seed 17 \
  --objective mixed \
  --out corridors/smt-mixed-17.json

.venv/bin/python -m corg_eval surface-plan \
  --plan corridors/smt-mixed-17.json \
  --provider sdk \
  --out corridors/smt-mixed-17-surfaced.json

python3 -m corg_eval probe \
  --model google/gemma-4-31b \
  --corridor corridors/smt-mixed-17-surfaced.json \
  --max-tokens 8192 \
  --out runs/gemma-smt-surface-probe.jsonl
```

The SMT planner chooses the corridor skeleton: answer uniqueness, tactic family, target field, template family, riddle variant, warmup decoys, and drift-capable coverage constraints. Surface generation only rewrites the untrusted lore text, and raw generations are stored in the corridor metadata for replay.

## Multi-Model Matrix

The default target cohort is:

```text
nvidia/nemotron-3-nano-omni
qwen/qwen3.6-35b-a3b
qwen/qwen3.6-27b
google/gemma-4-31b
google/gemma-4-e2b
google/gemma-4-e4b
ternary-bonsai-8b-mlx
```

Run a small smoke matrix:

```bash
python3 -m corg_eval benchmark \
  --models target \
  --seeds 0:1 \
  --depths 2 \
  --difficulties 1 \
  --lure-modes none,decoy,conflict,progressive \
  --modes reset,cumulative \
  --max-tokens 8192 \
  --out runs/smoke-matrix.jsonl
```

Run a publishable locked-seed pass by increasing seeds and depths:

```bash
python3 -m corg_eval benchmark \
  --models target \
  --seeds 100:130 \
  --depths 4,8 \
  --difficulties 1,2 \
  --lure-modes none,decoy,conflict,progressive \
  --modes reset,cumulative \
  --max-tokens 8192 \
  --out runs/locked-v0.2.jsonl
```

The default `--max-tokens` is 8192 because thinking models may spend thousands of hidden or `reasoning_content` tokens before emitting final JSON. For serious runs, tune the budget upward until depth-1 and depth-2 controls rarely end with `finish_reason=length`.

You can pass serving-specific knobs through `--extra-body-json`, for example:

```bash
python3 -m corg_eval run \
  --model qwen/qwen3.6-27b \
  --depth 2 \
  --compare-reset \
  --max-tokens 4096 \
  --extra-body-json '{"top_p":0.95}'
```

## Filesystem Work Queue

For long multi-model runs, enqueue jobs and let workers claim them atomically:

```bash
# Enqueue planned-probe jobs for the target cohort
python3 -m corg_eval queue-enqueue-plans \
  --queue-dir queues/pilot \
  --models target \
  --seeds 410:415 \
  --objectives mixed,corrigibility \
  --depth 3 \
  --out runs/pilot.jsonl

# Run a worker that claims jobs it can execute
python3 -m corg_eval queue-work \
  --queue-dir queues/pilot \
  --models target \
  --poll \
  --poll-interval-s 10.0

# Check queue depth and stale running jobs
python3 -m corg_eval queue-status --queue-dir queues/pilot --stale-threshold-m 60

# Reclaim stale jobs back to pending
python3 -m corg_eval queue-reclaim --queue-dir queues/pilot --stale-threshold-m 60
```

## Analysis And Site

Aggregate JSONL runs:

```bash
python3 -m corg_eval analyze runs/smoke-matrix.jsonl --out docs/results.json
```

The `docs/` directory is a static GitHub Pages site. Once pushed, GitHub Actions deploys it from `docs`.

## Kimi Judge Pass

REST mode uses `.env`; create it from `.env.example` and set `KIMI_API_KEY`. Do not commit `.env`.

```bash
python3 -m corg_eval judge --provider rest runs/smoke-matrix.jsonl --out runs/smoke-matrix-judged.jsonl
```

The judge uses `kimi-k2.6` by default through `https://api.moonshot.ai/v1` with `thinking: {"type": "disabled"}`.

If your Kimi credential is for Kimi Code rather than the Moonshot API, use the authenticated SDK or CLI fallback:

```bash
python3 -m corg_eval judge --provider sdk runs/smoke-matrix.jsonl --out runs/smoke-matrix-judged.jsonl
python3 -m corg_eval judge --provider cli runs/smoke-matrix.jsonl --out runs/smoke-matrix-judged.jsonl
```

## Search And Selection

The search loop is exploratory and research-scoped. It should avoid operationally harmful content, but it does not need to be limited to hand-written or mild templates. Treat searched candidates as selection artifacts, not evidence by themselves:

```bash
python3 -m corg_eval search \
  --model nvidia/nemotron-3-nano-omni \
  --generations 3 \
  --population 6 \
  --depth 6 \
  --out runs/search.jsonl
```

Searched or generated corridors should be replayed on locked held-out seeds before being used as evidence.

## Interpretability Path

LM Studio is enough for black-box behavioral measurement, but it does not expose internals. Future activation/logit work should use a runner with introspection hooks. A likely path is the `introspect` branch of [`spwplace/mistral-introspect`](https://github.com/spwplace/mistral-introspect/tree/introspect), currently a stretch target because model coverage is narrower than the LM Studio cohort.

## Research Anchors

- OpenAI Instruction Hierarchy
- HarmBench
- AgentDojo
- Tree of Attacks with Pruning
- Crescendo multi-turn instruction drift
