"""Tracer — pluggable observability backends for pipelines and agent loops.

A Tracer receives lifecycle hooks as a pipeline executes. Built-in
implementations:

- ``NoOpTracer`` — zero-cost default
- ``LoggingTracer`` — emits structured JSON via :class:`StructuredLogger`
- ``OTelTracer`` — creates OpenTelemetry run + step spans (soft dependency)
- ``CompositeTracer`` — fan-out to multiple tracers

Third-party tracers (LangSmith, Arize, Langfuse, etc.) implement ``Tracer``
and plug into ``Pipeline(tracer=...)`` or ``AgentLoop(tracer=...)``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from genblaze_core.observability.logger import StructuredLogger

if TYPE_CHECKING:
    from genblaze_core.models.step import Step
    from genblaze_core.observability.events import StreamEvent
    from genblaze_core.pipeline.result import PipelineResult

# Fallback logger used for tracer-internal errors — never propagated.
_trace_logger = logging.getLogger("genblaze.tracer")


class Tracer:
    """Base tracer interface — override the hooks you care about.

    Not an ABC because all hooks have safe no-op defaults: subclasses
    implement only what they need. Exceptions raised from any hook are
    swallowed by the pipeline — observability must never break execution.
    """

    def on_run_start(
        self,
        run_id: str,
        name: str | None,
        *,
        tenant_id: str | None = None,
        total_steps: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Called when a pipeline run begins."""

    def on_step_start(
        self,
        run_id: str,
        step: Step,
        *,
        step_index: int,
        total_steps: int,
    ) -> None:
        """Called immediately before a step is executed."""

    def on_event(self, event: StreamEvent) -> None:
        """Called for every StreamEvent (including progress ticks)."""

    def on_step_end(
        self,
        run_id: str,
        step: Step,
        *,
        duration_ms: float,
        step_index: int,
    ) -> None:
        """Called after a step completes (success or failure)."""

    def on_run_end(self, run_id: str, result: PipelineResult) -> None:
        """Called when a pipeline run finishes."""


class NoOpTracer(Tracer):
    """Default tracer — does nothing. Zero cost."""


class LoggingTracer(Tracer):
    """Emit structured JSON events via :class:`StructuredLogger`.

    Drop-in replacement for the legacy ``structured_log=True`` path.
    Default logger name is ``genblaze.pipeline`` to preserve the
    log-channel name callers already scrape.
    """

    def __init__(self, logger: StructuredLogger | None = None) -> None:
        self._logger = logger or StructuredLogger("genblaze.pipeline")

    def on_run_start(
        self,
        run_id: str,
        name: str | None,
        *,
        tenant_id: str | None = None,
        total_steps: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._logger.info(
            "pipeline.start",
            run_id=run_id,
            name=name,
            tenant_id=tenant_id,
            total_steps=total_steps,
        )

    def on_step_end(
        self,
        run_id: str,
        step: Step,
        *,
        duration_ms: float,
        step_index: int,
    ) -> None:
        self._logger.info(
            "step.completed",
            run_id=run_id,
            step_id=step.step_id,
            step_index=step_index,
            provider=step.provider,
            model=step.model,
            status=str(step.status),
            duration_ms=round(duration_ms, 1),
            cost_usd=step.cost_usd,
        )

    # Event types already emitted via imperative hooks above — skip them in
    # on_event so structured_log=True doesn't log the same transition twice.
    _HOOK_DUPES = frozenset(
        {
            "pipeline.started",
            "pipeline.completed",
            "pipeline.failed",
            "step.completed",
            "step.failed",
            "step.progress",  # noisy poll ticks
        }
    )

    def on_event(self, event: StreamEvent) -> None:
        if event.type in self._HOOK_DUPES:
            return
        self._logger.info(event.type, **event.to_dict())

    def on_run_end(self, run_id: str, result: PipelineResult) -> None:
        self._logger.info(
            "pipeline.complete",
            run_id=run_id,
            status=str(result.run.status),
            manifest_hash=result.manifest.canonical_hash,
        )


class OTelTracer(Tracer):
    """OpenTelemetry tracer — creates real spans for runs and steps.

    Requires the ``opentelemetry-api`` package to be installed. If not
    available, degrades silently to a no-op.
    """

    def __init__(self, tracer_name: str = "genblaze") -> None:
        self._tracer = None
        try:
            from opentelemetry import trace

            self._tracer = trace.get_tracer(tracer_name)
        except ImportError:
            _trace_logger.debug("OpenTelemetry not installed — OTelTracer is a no-op")
        self._run_spans: dict[str, Any] = {}
        self._step_spans: dict[str, Any] = {}

    def on_run_start(
        self,
        run_id: str,
        name: str | None,
        *,
        tenant_id: str | None = None,
        total_steps: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._tracer is None:
            return
        span = self._tracer.start_span(f"pipeline:{name or 'anonymous'}")
        span.set_attribute("genblaze.run_id", run_id)
        if tenant_id:
            span.set_attribute("genblaze.tenant_id", tenant_id)
        if total_steps is not None:
            span.set_attribute("genblaze.total_steps", total_steps)
        self._run_spans[run_id] = span

    def on_step_start(
        self,
        run_id: str,
        step: Step,
        *,
        step_index: int,
        total_steps: int,
    ) -> None:
        if self._tracer is None:
            return
        span = self._tracer.start_span(f"step:{step.provider}/{step.model}")
        span.set_attribute("genblaze.run_id", run_id)
        span.set_attribute("genblaze.step_id", step.step_id)
        span.set_attribute("genblaze.step_index", step_index)
        span.set_attribute("genblaze.provider", step.provider)
        span.set_attribute("genblaze.model", step.model)
        self._step_spans[step.step_id] = span

    def on_event(self, event: StreamEvent) -> None:
        # Attach progress ticks to the current step span as events.
        if event.type != "step.progress" or event.step_id is None:
            return
        span = self._step_spans.get(event.step_id)
        if span is None:
            return
        attrs: dict[str, Any] = {}
        if event.progress_pct is not None:
            attrs["progress_pct"] = event.progress_pct
        if event.preview_url:
            attrs["preview_url"] = event.preview_url
        span.add_event("progress", attributes=attrs)

    def on_step_end(
        self,
        run_id: str,
        step: Step,
        *,
        duration_ms: float,
        step_index: int,
    ) -> None:
        span = self._step_spans.pop(step.step_id, None)
        if span is None:
            return
        span.set_attribute("genblaze.duration_ms", duration_ms)
        span.set_attribute("genblaze.status", str(step.status))
        if step.cost_usd is not None:
            span.set_attribute("genblaze.cost_usd", step.cost_usd)
        if step.error:
            try:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, step.error)
            except ImportError:  # pragma: no cover
                pass
        span.end()

    def on_run_end(self, run_id: str, result: PipelineResult) -> None:
        span = self._run_spans.pop(run_id, None)
        if span is None:
            return
        span.set_attribute("genblaze.status", str(result.run.status))
        span.set_attribute("genblaze.manifest_hash", result.manifest.canonical_hash)
        span.end()


class CompositeTracer(Tracer):
    """Fan-out tracer — forwards every hook to all wrapped tracers.

    Exceptions from any child tracer are caught and logged; remaining
    tracers continue to receive events.
    """

    def __init__(self, tracers: list[Tracer]) -> None:
        self._tracers = list(tracers)

    def _fanout(self, method: str, *args: Any, **kwargs: Any) -> None:
        for t in self._tracers:
            safe_call(t, method, *args, **kwargs)

    def on_run_start(self, *args: Any, **kwargs: Any) -> None:
        self._fanout("on_run_start", *args, **kwargs)

    def on_step_start(self, *args: Any, **kwargs: Any) -> None:
        self._fanout("on_step_start", *args, **kwargs)

    def on_event(self, *args: Any, **kwargs: Any) -> None:
        self._fanout("on_event", *args, **kwargs)

    def on_step_end(self, *args: Any, **kwargs: Any) -> None:
        self._fanout("on_step_end", *args, **kwargs)

    def on_run_end(self, *args: Any, **kwargs: Any) -> None:
        self._fanout("on_run_end", *args, **kwargs)


def safe_call(tracer: Tracer, method: str, *args: Any, **kwargs: Any) -> None:
    """Invoke a tracer hook and swallow any exception — used by Pipeline.

    Exceptions are logged at WARNING so observability bugs are visible
    without breaking the pipeline.
    """
    try:
        getattr(tracer, method)(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        _trace_logger.warning("Tracer %s.%s failed: %s", type(tracer).__name__, method, exc)
