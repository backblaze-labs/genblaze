<!-- last_verified: 2026-04-24 -->
# Agents — Evaluate & Retry Loops

Generate → evaluate → refine until an output meets a quality bar. Every iteration is linked via `parent_run_id`, so the full reasoning trail is captured in the manifest lineage.

## Quickstart

```python
from genblaze_core import (
    Pipeline, AgentLoop, AgentContext,
    CallableEvaluator, EvaluationResult,
)
from genblaze_openai import DalleProvider

def build_pipeline(ctx: AgentContext) -> Pipeline:
    # Use prior feedback to refine the prompt.
    prompt = "a sunset over mountains"
    if ctx.last_evaluation and ctx.last_evaluation.feedback:
        prompt += f" — {ctx.last_evaluation.feedback}"
    return Pipeline(f"hero-iter-{ctx.iteration}").step(
        DalleProvider(), model="dall-e-3", prompt=prompt, modality="image",
    )

def judge(result):
    # Your vision/quality check here. Stub: always fail for demo.
    return EvaluationResult(
        passed=False,
        score=0.4,
        feedback="make the colors more saturated",
    )

loop = AgentLoop(
    build_pipeline,
    CallableEvaluator(judge),
    max_iterations=3,
)
out = loop.run()
print(f"Passed: {out.passed}, iters: {len(out.iterations)}, cost: ${out.total_cost_usd:.2f}")
```

## Evaluators

Pick the right base:

| Evaluator | Use when |
|-----------|----------|
| `CallableEvaluator(fn)` | One-off judge logic — inline function returning `EvaluationResult` or `bool` |
| `ThresholdEvaluator(score_fn, threshold)` | Numeric quality score with pass/fail cutoff |
| Custom `Evaluator` subclass | Complex logic, async, or multi-dimensional scoring |

### Vision-model judge

```python
from genblaze_core import Evaluator, EvaluationResult

class VisionJudge(Evaluator):
    def __init__(self, model: str):
        self._model = model  # e.g. your OpenAI vision wrapper

    def evaluate(self, result) -> EvaluationResult:
        url = result.run.steps[-1].assets[0].url
        score = self._model.score(url)  # returns 0.0–1.0
        return EvaluationResult(
            passed=score >= 0.8,
            score=score,
            feedback=self._model.feedback(url) if score < 0.8 else None,
        )
```

## AgentContext

The factory receives an `AgentContext`:

- `iteration` — 0-based index
- `prior_results` — list of all `PipelineResult`s so far
- `last_evaluation` — `EvaluationResult` from the previous iteration (None on iter 0)

Use `last_evaluation.feedback` to rewrite prompts, adjust parameters, or switch models across iterations.

## Manifest lineage

Every iteration after the first calls `Pipeline.from_result(prev)` automatically, so each manifest carries `parent_run_id` pointing back to the previous attempt. The full refinement chain is captured in provenance:

```
iter 0 (run_id=A, parent_run_id=None)
  ↓
iter 1 (run_id=B, parent_run_id=A)
  ↓
iter 2 (run_id=C, parent_run_id=B)
```

`parent_run_id` is excluded from the canonical hash — iteration links don't break integrity verification.

## Stopping conditions

The loop stops when **any** of these is true:
1. `evaluation.passed == True`
2. `max_iterations` reached
3. `stop_on_pipeline_failure=True` (default) and the pipeline itself errored

## Streaming agent events

Agent events are per-variant classes in the [StreamEvent discriminated union](streaming.md). Previously-bagged `data` fields are now proper attributes (`iteration`, `passed`, `score`, `feedback`, `total_cost_usd`) that narrow correctly when you branch on `event.type`.

```python
for event in loop.stream():
    if event.type == "agent.iteration.started":
        print(f"→ iter {event.iteration}")
    elif event.type == "agent.iteration.evaluated":
        print(f"  score={event.score} passed={event.passed}")
    elif event.type == "agent.completed":
        print(f"✓ final hash: {event.result.manifest.canonical_hash[:12]}")
```

Async via `loop.astream()`.

## Cost tracking

`AgentResult.total_cost_usd` sums `step.cost_usd` across every iteration's steps — so a 3-iter loop with a $0.04 image model reports $0.12.

## Memory footprint

`AgentLoop` retains every iteration's `PipelineResult` for the duration of the loop (the factory receives them via `AgentContext.prior_results`). Results hold URL references to assets — not the asset bytes — so per-iteration cost is typically 5–50 KB (manifest + step metadata). Even 100 iterations is a few MB.

If a factory only needs the most recent evaluation (the common case), it should read `ctx.last_evaluation` rather than walking `ctx.prior_results`. For very long loops or large manifests, cap `max_iterations` rather than relying on the evaluator to break early.

## Internals

- `libs/core/genblaze_core/agents/loop.py` — `AgentLoop`, `AgentResult`
- `libs/core/genblaze_core/agents/evaluator.py` — `Evaluator`, built-ins
