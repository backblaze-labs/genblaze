"""Tests for AgentLoop and the Evaluator hierarchy."""

from __future__ import annotations

from typing import Any

import pytest
from genblaze_core.agents import (
    AgentContext,
    AgentLoop,
    CallableEvaluator,
    EvaluationResult,
    Evaluator,
    ThresholdEvaluator,
)
from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider


class _Counter(BaseProvider):
    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.call = 0

    def submit(self, step, config=None) -> Any:
        self.call += 1
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/o.png", media_type="image/png"))
        step.cost_usd = 0.25
        return step


def _factory(provider: _Counter):
    def _build(ctx: AgentContext) -> Pipeline:
        return Pipeline(f"iter-{ctx.iteration}").step(provider, model="m", prompt="p")

    return _build


# --- Evaluators ---------------------------------------------------------------


def test_callable_evaluator_bool_return() -> None:
    ev = CallableEvaluator(lambda _r: True)
    p = Pipeline("t").step(_Counter(), model="m", prompt="p").run()
    out = ev.evaluate(p)
    assert out.passed is True


def test_callable_evaluator_result_return() -> None:
    ev = CallableEvaluator(
        lambda _r: EvaluationResult(passed=False, score=0.3, feedback="too dark")
    )
    p = Pipeline("t").step(_Counter(), model="m", prompt="p").run()
    out = ev.evaluate(p)
    assert out.passed is False
    assert out.score == 0.3
    assert out.feedback == "too dark"


def test_threshold_evaluator_pass() -> None:
    ev = ThresholdEvaluator(score_fn=lambda _r: 0.9, threshold=0.8)
    p = Pipeline("t").step(_Counter(), model="m", prompt="p").run()
    out = ev.evaluate(p)
    assert out.passed is True
    assert out.score == 0.9


def test_threshold_evaluator_fail_with_feedback() -> None:
    ev = ThresholdEvaluator(
        score_fn=lambda _r: 0.5,
        threshold=0.8,
        feedback_fn=lambda _r, s: f"score {s} below 0.8",
    )
    p = Pipeline("t").step(_Counter(), model="m", prompt="p").run()
    out = ev.evaluate(p)
    assert out.passed is False
    assert "below" in (out.feedback or "")


# --- AgentLoop ----------------------------------------------------------------


def test_agent_loop_stops_on_pass() -> None:
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: True),
        max_iterations=5,
    )
    out = loop.run()
    assert out.passed is True
    assert len(out.iterations) == 1
    assert provider.call == 1


def test_agent_loop_retries_until_threshold() -> None:
    """Loop should retry when evaluator returns passed=False."""
    provider = _Counter()
    seen: list[int] = []

    def _eval(result):
        seen.append(provider.call)
        # Pass on the third call
        return EvaluationResult(passed=provider.call >= 3, score=float(provider.call))

    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(_eval),
        max_iterations=5,
    )
    out = loop.run()
    assert out.passed is True
    assert len(out.iterations) == 3
    assert provider.call == 3


def test_agent_loop_respects_max_iterations() -> None:
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: False),  # never passes
        max_iterations=2,
    )
    out = loop.run()
    assert out.passed is False
    assert len(out.iterations) == 2


def test_agent_loop_sums_cost_across_iterations() -> None:
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: False),
        max_iterations=3,
    )
    out = loop.run()
    # 3 iterations × 1 step × $0.25 per step = $0.75
    assert out.total_cost_usd == pytest.approx(0.75)


def test_agent_loop_links_iterations_via_parent_run_id() -> None:
    """Each iteration after the first should carry parent_run_id from prior."""
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: False),
        max_iterations=3,
    )
    out = loop.run()
    # First iteration has no parent
    assert out.iterations[0].result.run.parent_run_id is None
    # Subsequent iterations link back
    for prev, curr in zip(out.iterations, out.iterations[1:], strict=False):
        assert curr.result.run.parent_run_id == prev.result.run.run_id


def test_agent_loop_rejects_zero_max_iterations() -> None:
    with pytest.raises(GenblazeError, match="max_iterations"):
        AgentLoop(_factory(_Counter()), CallableEvaluator(lambda _r: True), max_iterations=0)


def test_agent_loop_context_carries_feedback() -> None:
    """Factory receives last_evaluation.feedback to drive prompt refinement."""
    provider = _Counter()
    seen_feedback: list[str | None] = []

    def _build(ctx: AgentContext) -> Pipeline:
        seen_feedback.append(ctx.last_evaluation.feedback if ctx.last_evaluation else None)
        return Pipeline(f"it-{ctx.iteration}").step(provider, model="m", prompt="p")

    def _eval(result):
        return EvaluationResult(
            passed=provider.call >= 2,
            score=float(provider.call),
            feedback=f"try again, attempt={provider.call}",
        )

    loop = AgentLoop(_build, CallableEvaluator(_eval), max_iterations=3)
    out = loop.run()
    assert out.passed is True
    # First iter had no feedback; second got the first evaluation's feedback
    assert seen_feedback[0] is None
    assert seen_feedback[1] is not None
    assert "attempt=1" in seen_feedback[1]


def test_agent_loop_stops_on_pipeline_failure() -> None:
    """stop_on_pipeline_failure=True prevents retry when the pipeline itself errors."""

    class _Boom(BaseProvider):
        name = "boom"

        def submit(self, step, config=None):
            raise RuntimeError("always fails")

        def poll(self, prediction_id, config=None) -> bool:  # pragma: no cover
            return True

        def fetch_output(self, prediction_id, step):  # pragma: no cover
            return step

    def _build(ctx):
        return Pipeline(f"it-{ctx.iteration}").step(_Boom(), model="m", prompt="p")

    loop = AgentLoop(
        _build,
        CallableEvaluator(lambda _r: False),
        max_iterations=5,
        stop_on_pipeline_failure=True,
    )
    out = loop.run()
    assert len(out.iterations) == 1  # did not retry after pipeline failed
    assert out.passed is False
    assert out.final.run.steps[0].status == StepStatus.FAILED


# --- Async + streaming --------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_arun() -> None:
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: True),
        max_iterations=3,
    )
    out = await loop.arun()
    assert out.passed is True
    assert len(out.iterations) == 1


def test_agent_loop_stream_emits_iteration_events() -> None:
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: provider.call >= 2),
        max_iterations=3,
    )
    events = list(loop.stream())
    types = [e.type for e in events]
    assert "agent.iteration.started" in types
    assert "agent.iteration.evaluated" in types
    assert types[-1] == "agent.completed"


# --- Async evaluator ----------------------------------------------------------


class _AsyncEval(Evaluator):
    def __init__(self) -> None:
        self.sync_calls = 0
        self.async_calls = 0

    def evaluate(self, result):
        self.sync_calls += 1
        return EvaluationResult(passed=True)

    async def aevaluate(self, result):
        self.async_calls += 1
        return EvaluationResult(passed=True)


@pytest.mark.asyncio
async def test_agent_loop_uses_async_eval_in_arun() -> None:
    provider = _Counter()
    evaluator = _AsyncEval()
    loop = AgentLoop(_factory(provider), evaluator, max_iterations=1)
    await loop.arun()
    assert evaluator.async_calls == 1
    assert evaluator.sync_calls == 0
