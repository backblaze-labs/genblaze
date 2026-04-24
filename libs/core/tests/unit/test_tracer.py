"""Tests for the Tracer abstraction and built-in backends."""

from __future__ import annotations

from typing import Any

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.observability.events import StepProgressEvent
from genblaze_core.observability.tracer import (
    CompositeTracer,
    LoggingTracer,
    NoOpTracer,
    Tracer,
    safe_call,
)
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider


class _OKProvider(BaseProvider):
    name = "mock"

    def submit(self, step, config=None) -> Any:
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
        return step


class _RecordingTracer(Tracer):
    """Records every hook call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def on_run_start(self, *args, **kwargs) -> None:
        self.calls.append(("on_run_start", args, kwargs))

    def on_step_start(self, *args, **kwargs) -> None:
        self.calls.append(("on_step_start", args, kwargs))

    def on_event(self, *args, **kwargs) -> None:
        self.calls.append(("on_event", args, kwargs))

    def on_step_end(self, *args, **kwargs) -> None:
        self.calls.append(("on_step_end", args, kwargs))

    def on_run_end(self, *args, **kwargs) -> None:
        self.calls.append(("on_run_end", args, kwargs))


class _BoomTracer(Tracer):
    """Every hook raises — verifies pipelines survive buggy tracers."""

    def on_run_start(self, *args, **kwargs) -> None:
        raise RuntimeError("boom run start")

    def on_step_start(self, *args, **kwargs) -> None:
        raise RuntimeError("boom step start")

    def on_event(self, *args, **kwargs) -> None:
        raise RuntimeError("boom event")

    def on_step_end(self, *args, **kwargs) -> None:
        raise RuntimeError("boom step end")

    def on_run_end(self, *args, **kwargs) -> None:
        raise RuntimeError("boom run end")


# --- Tracer wiring ------------------------------------------------------------


def test_noop_tracer_is_safe_default() -> None:
    """NoOpTracer accepts every hook with no effect."""
    t = NoOpTracer()
    t.on_run_start("r", "n")
    t.on_event(StepProgressEvent(step_id="s1", provider="p", model="m"))
    t.on_run_end("r", None)  # type: ignore[arg-type]


def test_pipeline_invokes_all_lifecycle_hooks() -> None:
    tracer = _RecordingTracer()
    Pipeline("t", tracer=tracer).step(_OKProvider(), model="m", prompt="p").run()

    names = [c[0] for c in tracer.calls]
    assert "on_run_start" in names
    assert "on_step_start" in names
    assert "on_step_end" in names
    assert "on_run_end" in names
    assert names.count("on_run_start") == 1
    assert names.count("on_run_end") == 1
    assert names.count("on_step_start") == 1
    assert names.count("on_step_end") == 1


def test_tracer_receives_stream_events() -> None:
    tracer = _RecordingTracer()
    Pipeline("t", tracer=tracer).step(_OKProvider(), model="m", prompt="p").run()
    event_calls = [c for c in tracer.calls if c[0] == "on_event"]
    types = [c[1][0].type for c in event_calls]
    assert "pipeline.started" in types
    assert "pipeline.completed" in types


def test_buggy_tracer_does_not_break_pipeline() -> None:
    """Tracer exceptions must be swallowed — observability must never break jobs."""
    result = Pipeline("t", tracer=_BoomTracer()).step(_OKProvider(), model="m", prompt="p").run()
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


def test_composite_tracer_fans_out() -> None:
    t1 = _RecordingTracer()
    t2 = _RecordingTracer()
    composite = CompositeTracer([t1, t2])
    Pipeline("t", tracer=composite).step(_OKProvider(), model="m", prompt="p").run()

    assert len(t1.calls) > 0
    assert len(t2.calls) > 0
    # Same call counts — every hook gets fanned to both
    assert [c[0] for c in t1.calls] == [c[0] for c in t2.calls]


def test_composite_isolates_failures() -> None:
    """If one child tracer raises, others still receive the hook."""
    t1 = _BoomTracer()
    t2 = _RecordingTracer()
    composite = CompositeTracer([t1, t2])
    Pipeline("t", tracer=composite).step(_OKProvider(), model="m", prompt="p").run()
    # t2 still got events despite t1 raising
    assert any(c[0] == "on_run_start" for c in t2.calls)


def test_safe_call_swallows_exceptions() -> None:
    safe_call(_BoomTracer(), "on_run_start", "r", "n")  # must not raise


# --- LoggingTracer preserves structured_log=True behavior ---------------------


def test_structured_log_resolves_to_logging_tracer(caplog) -> None:
    """structured_log=True activates LoggingTracer so JSON events fire."""
    import json
    import logging

    caplog.set_level(logging.INFO, logger="genblaze.pipeline")
    caplog.set_level(logging.INFO, logger="genblaze.tracer")

    Pipeline("t", structured_log=True).step(_OKProvider(), model="m", prompt="p").run()

    events = []
    for rec in caplog.records:
        try:
            events.append(json.loads(rec.getMessage()))
        except json.JSONDecodeError:
            continue
    names = [e.get("event") for e in events]
    assert "pipeline.start" in names
    assert "pipeline.complete" in names


def test_logging_tracer_explicit() -> None:
    """Constructing LoggingTracer directly works outside the legacy flag."""
    tracer = LoggingTracer()
    result = Pipeline("t", tracer=tracer).step(_OKProvider(), model="m", prompt="p").run()
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


def test_pipeline_tracer_setter() -> None:
    """.tracer() attaches a tracer after construction."""
    tracer = _RecordingTracer()
    pipe = Pipeline("t").step(_OKProvider(), model="m", prompt="p")
    pipe.tracer(tracer)
    pipe.run()
    assert any(c[0] == "on_run_start" for c in tracer.calls)


# --- Lifecycle invariants: start/end hooks always pair up ---------------------


class _RaisingProvider(BaseProvider):
    """Provider that raises from invoke() via a non-retryable error path."""

    name = "raising"

    def submit(self, step, config=None):
        raise RuntimeError("kaboom")

    def poll(self, prediction_id, config=None) -> bool:  # pragma: no cover
        return True

    def fetch_output(self, prediction_id, step):  # pragma: no cover
        return step


def _hook_sequence(tracer: _RecordingTracer) -> list[str]:
    return [c[0] for c in tracer.calls]


def test_on_step_end_fires_when_step_fails() -> None:
    """Regression: on_step_start must always be paired with on_step_end."""
    tracer = _RecordingTracer()
    Pipeline("t", tracer=tracer).step(_RaisingProvider(), model="m", prompt="p").run()

    seq = _hook_sequence(tracer)
    assert seq.count("on_step_start") == seq.count("on_step_end") == 1


def test_on_run_end_fires_on_pipeline_timeout() -> None:
    """Regression: PipelineTimeoutError used to skip on_run_end, leaking tracer state."""
    from genblaze_core.exceptions import PipelineTimeoutError

    tracer = _RecordingTracer()
    # pipeline_timeout=0 triggers an immediate timeout on the first pre-step check
    pipe = Pipeline("t", tracer=tracer).step(_OKProvider(), model="m", prompt="p")
    try:
        pipe.run(pipeline_timeout=0.0)
    except PipelineTimeoutError:
        pass

    seq = _hook_sequence(tracer)
    assert seq.count("on_run_start") == seq.count("on_run_end") == 1


def test_otel_tracer_no_leak_on_timeout() -> None:
    """OTelTracer's internal run_spans dict must empty even when run() aborts."""
    from genblaze_core.exceptions import PipelineTimeoutError
    from genblaze_core.observability.tracer import OTelTracer

    tracer = OTelTracer()
    pipe = Pipeline("t", tracer=tracer).step(_OKProvider(), model="m", prompt="p")
    try:
        pipe.run(pipeline_timeout=0.0)
    except PipelineTimeoutError:
        pass

    # Even if the OTel SDK isn't installed (self._tracer is None), the dicts
    # stay empty. If the SDK is installed, on_run_end popped the entry.
    assert tracer._run_spans == {}
    assert tracer._step_spans == {}
