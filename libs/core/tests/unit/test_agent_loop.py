"""Tests for AgentLoop and the Evaluator hierarchy."""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_agent_loop_astream_emits_iteration_events() -> None:
    """Async sibling of test_agent_loop_stream_emits_iteration_events —
    astream() had no coverage at all (#51)."""
    provider = _Counter()
    loop = AgentLoop(
        _factory(provider),
        CallableEvaluator(lambda _r: provider.call >= 2),
        max_iterations=3,
    )
    events = []
    async for ev in loop.astream():
        events.append(ev)
    types = [e.type for e in events]
    assert "agent.iteration.started" in types
    assert "agent.iteration.evaluated" in types
    assert types[-1] == "agent.completed"


@pytest.mark.asyncio
async def test_agent_loop_astream_early_break_cancels_worker() -> None:
    """Breaking early from astream() cancels the worker task cleanly —
    mirrors Pipeline's test_astream_early_break_cancels_worker, but for
    AgentLoop, whose async cancel path previously had no coverage (#51).

    Step 2's provider blocks (in a to_thread-offloaded submit()) on an
    Event this test only releases AFTER the assertion — if early break
    had awaited the worker task to completion instead of cancelling it,
    the outer ``asyncio.wait_for`` below would time out.
    """
    import threading
    from contextlib import aclosing

    release = threading.Event()

    class _BlockingAsyncProvider(BaseProvider):
        name = "blocking-async"

        def submit(self, step, config=None) -> Any:
            release.wait(timeout=5.0)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/o.png", media_type="image/png"))
            return step

    def _build_slow(ctx: AgentContext) -> Pipeline:
        # chain=True forces sequential execution so step 1's step.completed
        # fires (and can be broken on) before step 2 ever starts — the
        # default concurrent mode runs both steps in parallel and only
        # emits step.completed for the whole batch once ALL steps resolve.
        return (
            Pipeline(f"it-{ctx.iteration}", chain=True)
            .step(_Counter(), model="m", prompt="p1")
            .step(_BlockingAsyncProvider(), model="m", prompt="p2")
        )

    loop = AgentLoop(_build_slow, CallableEvaluator(lambda _r: True), max_iterations=3)

    async def _consume() -> None:
        # aclosing() explicitly drives the async generator's cleanup on
        # break — a bare `async for ... break` only decrefs the generator,
        # and unlike sync generators, an abandoned async generator's
        # finally block is not guaranteed to run promptly without an
        # explicit aclose().
        async with aclosing(loop.astream()) as stream:
            async for event in stream:
                if event.type == "step.completed":
                    break  # bail after the first pipeline step

    try:
        await asyncio.wait_for(_consume(), timeout=2.0)
    except TimeoutError:
        pytest.fail("astream() blocked on early break: step 2 was still in flight")
    finally:
        release.set()  # let the cancelled/abandoned worker wind down


# --- Concurrent stream()/astream() isolation (#79) -----------------------------


def test_concurrent_agent_loop_streams_do_not_cross_deliver() -> None:
    """Two simultaneous stream() calls on ONE AgentLoop instance must each
    deliver their own isolated, complete event sequence — end-to-end
    coverage of the public stream() contract under concurrent use.

    (The single-iteration shape here happens to isolate pipeline-level
    events even under the old shared-attribute design, since each iteration
    captures the emitter once via attach_emitter(); see
    test_agent_loop_emitter_slot_isolated_across_concurrent_iterations
    below for a deterministic reproduction of the actual agent-level race
    fixed by #79 — the one only exercisable across >= 2 iterations with
    genuine thread overlap.)
    """
    import threading

    gate = threading.Event()

    class _GatedProvider(BaseProvider):
        name = "gated"

        def submit(self, step, config=None) -> Any:
            gate.wait(timeout=5.0)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/o.png", media_type="image/png"))
            return step

    def _build(ctx: AgentContext) -> Pipeline:
        return Pipeline(f"it-{ctx.iteration}").step(_GatedProvider(), model="m", prompt="p")

    loop = AgentLoop(_build, CallableEvaluator(lambda _r: True), max_iterations=1)

    gen_a = loop.stream()
    gen_b = loop.stream()

    started_a = next(gen_a)
    started_b = next(gen_b)
    assert started_a.type == "agent.iteration.started"
    assert started_b.type == "agent.iteration.started"

    gate.set()  # let both submit() calls proceed concurrently

    events_a = [started_a, *list(gen_a)]
    events_b = [started_b, *list(gen_b)]

    # Pipeline-level events carry run_id; agent-level events don't. Each
    # stream's pipeline-scoped run_ids must be disjoint from the other's —
    # cross-delivery would show a foreign run_id mixed into one stream.
    run_ids_a = {e.run_id for e in events_a if getattr(e, "run_id", None)}
    run_ids_b = {e.run_id for e in events_b if getattr(e, "run_id", None)}
    assert run_ids_a, "stream A saw no pipeline-scoped events"
    assert run_ids_b, "stream B saw no pipeline-scoped events"
    assert run_ids_a.isdisjoint(run_ids_b)
    assert events_a[-1].type == "agent.completed"
    assert events_b[-1].type == "agent.completed"


def test_agent_loop_emitter_slot_isolated_across_concurrent_iterations() -> None:
    """The emitter AgentLoop reads for agent-level events and for attaching
    to each iteration's freshly-built pipeline must be the one installed on
    the CALLING thread, not whichever thread last called stream()/astream().

    Regression: ``self._emitter`` was a single mutable instance attribute.
    Two threads racing to install their own emitter before reading it back
    (via ``_emit_iteration_start`` / ``_build_pipeline``) would have one
    thread observe the OTHER thread's emitter — a `threading.Barrier` makes
    this deterministic instead of a timing-dependent flake.
    """
    import queue
    import threading

    from genblaze_core.pipeline.streaming import QueueEmitter

    provider = _Counter()
    loop = AgentLoop(_factory(provider), CallableEvaluator(lambda _r: True), max_iterations=1)

    q_a: queue.Queue = queue.Queue()
    q_b: queue.Queue = queue.Queue()
    emitter_a = QueueEmitter(q_a)
    emitter_b = QueueEmitter(q_b)
    barrier = threading.Barrier(2)

    def _install_then_build(emitter: QueueEmitter, results: list) -> None:
        loop._emitter_slot.set(emitter)
        # Both threads must have installed their OWN emitter before either
        # is allowed to read it back — guarantees genuine overlap instead
        # of relying on scheduler luck.
        barrier.wait(timeout=5.0)
        ctx = loop._make_context(0, [])
        loop._emit_iteration_start(ctx)
        pipeline = loop._build_pipeline(ctx)
        # Read back _event_emitter on THIS thread — a contextvar-backed
        # slot is only visible on the thread that set it, so the main test
        # thread reading it after join() would see nothing (correctly),
        # not the bug we're checking for.
        results.append(pipeline._event_emitter is emitter)

    results_a: list[bool] = []
    results_b: list[bool] = []
    t_a = threading.Thread(target=_install_then_build, args=(emitter_a, results_a))
    t_b = threading.Thread(target=_install_then_build, args=(emitter_b, results_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)

    # Each thread's freshly-built pipeline must have captured ITS OWN
    # emitter, not whichever thread happened to install last.
    assert results_a == [True], "thread A's pipeline did not capture its own emitter"
    assert results_b == [True], "thread B's pipeline did not capture its own emitter"
    # And each thread's own agent.iteration.started landed on its own queue.
    assert q_a.get_nowait().type == "agent.iteration.started"
    assert q_b.get_nowait().type == "agent.iteration.started"


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
