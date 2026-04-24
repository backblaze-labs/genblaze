"""Agent loop example — generate, evaluate, retry with refinement.

Runs entirely against a mock provider so you can try it out without any
API keys. Demonstrates:

- Building a fresh Pipeline per iteration via a factory
- Feeding evaluator feedback back into the next prompt
- Streaming agent + pipeline events as they happen
- Per-iteration manifest lineage via parent_run_id
"""

from __future__ import annotations

from genblaze_core import (
    AgentContext,
    AgentLoop,
    CallableEvaluator,
    EvaluationResult,
    MockProvider,
    Pipeline,
)

provider = MockProvider(cost_usd=0.04)


def build_pipeline(ctx: AgentContext) -> Pipeline:
    """Build the next iteration's pipeline, folding in prior feedback.

    Records ``ctx.iteration`` in the step params so the evaluator can
    gate on attempt number. ``params`` survives normalization and ends
    up on the executed Step, which is what the evaluator inspects.
    """
    base_prompt = "a serene mountain lake at sunrise"
    if ctx.last_evaluation and ctx.last_evaluation.feedback:
        prompt = f"{base_prompt} — {ctx.last_evaluation.feedback}"
    else:
        prompt = base_prompt
    pipe = Pipeline(f"hero-iter-{ctx.iteration}").step(
        provider,
        model="mock-v1",
        prompt=prompt,
        _attempt=ctx.iteration,
    )
    return pipe


def judge_by_iteration(result) -> EvaluationResult:
    """Mock quality check that passes on the 3rd attempt."""
    attempt = result.run.steps[-1].params.get("_attempt", 0)
    passed = attempt >= 2
    return EvaluationResult(
        passed=passed,
        score=0.3 + 0.3 * attempt,
        feedback="try with warmer lighting" if not passed else None,
    )


loop = AgentLoop(
    build_pipeline,
    CallableEvaluator(judge_by_iteration),
    max_iterations=4,
)

print("=== Streaming agent events ===")
for event in loop.stream():
    # Per-variant narrowing — the discriminated union gives each branch
    # a precise type, so the fields accessed below are statically known.
    if event.type == "agent.iteration.started":
        print(f"→ iter {event.iteration}: {event.message or '(no feedback)'}")
    elif event.type == "step.completed":
        print(f"  step ok: {event.provider}/{event.model}")
    elif event.type == "agent.iteration.evaluated":
        score_str = f"{event.score:.2f}" if event.score is not None else "n/a"
        print(f"  evaluated: score={score_str} passed={event.passed} feedback={event.feedback!r}")
    elif event.type == "agent.completed":
        total_cost = event.total_cost_usd or 0.0
        print(
            f"\n=== Done: passed={event.passed}"
            f" iterations={event.iterations}"
            f" total_cost=${total_cost:.2f} ==="
        )
        print(f"Final manifest hash: {event.result.manifest.canonical_hash[:16]}...")

print("\n=== Manifest lineage ===")
out = loop.run()  # non-streaming, for the lineage dump
for it in out.iterations:
    parent = it.result.run.parent_run_id
    parent_short = parent[:8] + "..." if parent else "(root)"
    print(f"iter {it.index}: run_id={it.result.run.run_id[:8]}... parent={parent_short}")
