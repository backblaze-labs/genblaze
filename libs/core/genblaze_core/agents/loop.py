"""AgentLoop — iterative generate → evaluate → refine until a quality bar is met.

Composes an existing :class:`Pipeline` factory with an :class:`Evaluator`.
Each iteration:

1. Build a fresh :class:`Pipeline` via ``pipeline_factory(ctx)``
2. Execute it (``run`` or ``arun``) — linked to the previous iteration via
   ``Pipeline.from_result(prev)`` for manifest lineage
3. Evaluate the result
4. If passed (or max iterations reached), stop
5. Otherwise loop with the new ``AgentContext``

Streams :class:`StreamEvent` instances (``agent.iteration.started``,
``agent.iteration.evaluated``, ``agent.completed``) layered on top of the
underlying pipeline events.
"""

from __future__ import annotations

import asyncio
import logging
import queue
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from genblaze_core.agents.evaluator import EvaluationResult, Evaluator
from genblaze_core.exceptions import GenblazeError
from genblaze_core.observability.events import StreamEvent
from genblaze_core.observability.tracer import NoOpTracer, Tracer, safe_call
from genblaze_core.pipeline.streaming import QueueEmitter

if TYPE_CHECKING:
    from collections.abc import Callable

    from genblaze_core.pipeline.pipeline import Pipeline
    from genblaze_core.pipeline.result import PipelineResult

logger = logging.getLogger("genblaze.agent")


@dataclass
class AgentContext:
    """Snapshot passed to ``pipeline_factory`` to build each iteration.

    Attributes:
        iteration: 0-based iteration index.
        prior_results: All pipeline results produced so far.
        last_evaluation: Evaluation for the previous iteration (None on first).
    """

    iteration: int
    prior_results: list[PipelineResult] = field(default_factory=list)
    last_evaluation: EvaluationResult | None = None


@dataclass
class AgentIteration:
    """Record of a single loop iteration."""

    index: int
    result: PipelineResult
    evaluation: EvaluationResult


@dataclass
class AgentResult:
    """Final outcome of an agent loop.

    Attributes:
        iterations: All iterations attempted, in order.
        final: The last iteration's pipeline result (success or the best attempt).
        passed: Whether the final iteration passed evaluation.
        total_cost_usd: Sum of ``step.cost_usd`` across every iteration's steps.
    """

    iterations: list[AgentIteration]
    final: PipelineResult
    passed: bool
    total_cost_usd: float = 0.0


class AgentLoop:
    """Generate → evaluate → refine loop.

    Args:
        pipeline_factory: Builds a fresh :class:`Pipeline` for each iteration.
            Receives an :class:`AgentContext` carrying prior results + last
            evaluation feedback. The factory owns prompt/parameter refinement.
        evaluator: Judges whether each result meets quality requirements.
        max_iterations: Hard cap on attempts. Defaults to 3.
        tracer: Optional tracer for observability. Defaults to NoOp.
        stop_on_pipeline_failure: If True (default), stop looping when the
            pipeline itself errors (not just low quality). Prevents infinite
            retry of persistent failures (auth, network, model not found).
    """

    def __init__(
        self,
        pipeline_factory: Callable[[AgentContext], Pipeline],
        evaluator: Evaluator,
        *,
        max_iterations: int = 3,
        tracer: Tracer | None = None,
        stop_on_pipeline_failure: bool = True,
    ) -> None:
        if max_iterations < 1:
            raise GenblazeError("max_iterations must be >= 1")
        self._factory = pipeline_factory
        self._evaluator = evaluator
        self._max_iterations = max_iterations
        self._tracer = tracer or NoOpTracer()
        self._stop_on_failure = stop_on_pipeline_failure
        # Emitter installed by stream()/astream() so pipeline events flow out.
        self._emitter: QueueEmitter | None = None

    # ------------------------------------------------------------------
    # Public API — run / arun / stream / astream
    # ------------------------------------------------------------------

    def run(self, **run_kwargs: Any) -> AgentResult:
        """Synchronously iterate until passed or max_iterations reached."""
        iterations: list[AgentIteration] = []
        for i in range(self._max_iterations):
            ctx = self._make_context(i, iterations)
            self._emit_iteration_start(ctx)
            pipeline = self._build_pipeline(ctx)
            result = pipeline.run(**run_kwargs)
            evaluation = self._evaluator.evaluate(result)
            iterations.append(AgentIteration(index=i, result=result, evaluation=evaluation))
            self._emit_iteration_evaluated(i, result, evaluation)
            if self._should_stop(result, evaluation):
                break

        return self._finalize(iterations)

    async def arun(self, **run_kwargs: Any) -> AgentResult:
        """Async version of :meth:`run`."""
        iterations: list[AgentIteration] = []
        for i in range(self._max_iterations):
            ctx = self._make_context(i, iterations)
            self._emit_iteration_start(ctx)
            pipeline = self._build_pipeline(ctx)
            result = await pipeline.arun(**run_kwargs)
            evaluation = await self._evaluator.aevaluate(result)
            iterations.append(AgentIteration(index=i, result=result, evaluation=evaluation))
            self._emit_iteration_evaluated(i, result, evaluation)
            if self._should_stop(result, evaluation):
                break

        return self._finalize(iterations)

    def stream(self, **run_kwargs: Any):
        """Run the loop in a thread and yield StreamEvents as they arrive.

        Emits per-pipeline events (piped from each iteration's pipeline) and
        agent-level events (``agent.iteration.started``,
        ``agent.iteration.evaluated``, ``agent.completed``).

        Early break: if the caller abandons iteration, the worker keeps
        running as a daemon thread and its remaining events are discarded.
        """
        import threading

        from genblaze_core.pipeline.streaming import drain_queue_sync

        q: queue.Queue = queue.Queue()
        self._emitter = QueueEmitter(q)
        exc_box: list[BaseException] = []
        done = threading.Event()

        def _worker() -> None:
            try:
                final = self.run(**run_kwargs)
                self._emitter.put(self._agent_completed_event(final))
            except BaseException as exc:  # noqa: BLE001
                exc_box.append(exc)
            finally:
                self._emitter.close()
                done.set()

        t = threading.Thread(target=_worker, daemon=True, name="genblaze-agent-stream")
        t.start()
        try:
            yield from drain_queue_sync(q)
        finally:
            self._emitter = None
            if done.is_set():
                t.join()
                if exc_box:
                    raise exc_box[0]

    async def astream(self, **run_kwargs: Any):
        """Async version of :meth:`stream`.

        Early break: cancels the worker task; in-flight awaits propagate
        :class:`asyncio.CancelledError` and the exception is suppressed.
        """
        from genblaze_core.pipeline.streaming import drain_queue_async

        q: asyncio.Queue = asyncio.Queue()
        self._emitter = QueueEmitter(q)

        async def _worker() -> None:
            try:
                final = await self.arun(**run_kwargs)
                self._emitter.put(self._agent_completed_event(final))
            finally:
                self._emitter.close()

        task = asyncio.create_task(_worker())
        try:
            async for ev in drain_queue_async(q):
                yield ev
        finally:
            self._emitter = None
            if task.done():
                await task
            else:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                    logger.debug("astream worker aborted: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_context(self, i: int, iterations: list[AgentIteration]) -> AgentContext:
        return AgentContext(
            iteration=i,
            prior_results=[it.result for it in iterations],
            last_evaluation=iterations[-1].evaluation if iterations else None,
        )

    def _build_pipeline(self, ctx: AgentContext) -> Pipeline:
        """Invoke the factory, attach tracer + emitter, and inherit lineage."""
        pipeline = self._factory(ctx)
        pipeline.tracer(self._tracer)
        if ctx.prior_results:
            pipeline.from_result(ctx.prior_results[-1])
        if self._emitter is not None:
            pipeline.attach_emitter(self._emitter)
        return pipeline

    def _should_stop(self, result: PipelineResult, evaluation: EvaluationResult) -> bool:
        if evaluation.passed:
            return True
        if self._stop_on_failure and any(s.error for s in result.run.steps):
            logger.info("Agent loop stopping: pipeline step errored")
            return True
        return False

    def _emit_iteration_start(self, ctx: AgentContext) -> None:
        event = StreamEvent(
            type="agent.iteration.started",
            data={"iteration": ctx.iteration, "total": self._max_iterations},
            message=ctx.last_evaluation.feedback if ctx.last_evaluation else None,
        )
        safe_call(self._tracer, "on_event", event)
        if self._emitter is not None:
            self._emitter.put(event)

    def _emit_iteration_evaluated(
        self,
        iteration: int,
        result: PipelineResult,
        evaluation: EvaluationResult,
    ) -> None:
        event = StreamEvent(
            type="agent.iteration.evaluated",
            result=result,
            data={
                "iteration": iteration,
                "passed": evaluation.passed,
                "score": evaluation.score,
                "feedback": evaluation.feedback,
            },
        )
        safe_call(self._tracer, "on_event", event)
        if self._emitter is not None:
            self._emitter.put(event)

    def _agent_completed_event(self, final: AgentResult) -> StreamEvent:
        return StreamEvent(
            type="agent.completed",
            result=final.final,
            data={
                "passed": final.passed,
                "iterations": len(final.iterations),
                "total_cost_usd": final.total_cost_usd,
            },
        )

    def _finalize(self, iterations: list[AgentIteration]) -> AgentResult:
        if not iterations:  # pragma: no cover — max_iterations >= 1 guaranteed
            raise GenblazeError("Agent loop produced no iterations")
        final = iterations[-1]
        total_cost = sum(step.cost_usd or 0.0 for it in iterations for step in it.result.run.steps)
        return AgentResult(
            iterations=iterations,
            final=final.result,
            passed=final.evaluation.passed,
            total_cost_usd=total_cost,
        )
