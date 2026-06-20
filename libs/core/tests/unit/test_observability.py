"""Tests for observability — StepSpan and StructuredLogger integration."""

from __future__ import annotations

import json
import logging
import sys
import types
from typing import Any

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.observability.logger import StructuredLogger
from genblaze_core.observability.span import StepSpan
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider
from genblaze_core.runnable.config import RunnableConfig


class MockProvider(BaseProvider):
    """Provider that always succeeds with a single asset."""

    name = "mock"

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        return "pred-123"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
        return step


class _FailingAttributeSpan:
    """Fake OTel span that can reject selected attributes."""

    def __init__(self, fail_keys: set[str]) -> None:
        self.fail_keys = fail_keys
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        if key in self.fail_keys:
            raise TypeError(f"unsupported attribute {key}")
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.attributes["recorded_exception"] = type(exc).__name__

    def set_status(self, status: Any, description: str) -> None:
        self.attributes["status"] = (status, description)

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self, span: _FailingAttributeSpan) -> None:
        self.span = span

    def start_span(self, name: str) -> _FailingAttributeSpan:
        self.span.attributes["span_name"] = name
        return self.span


def _install_fake_otel(monkeypatch, span: _FailingAttributeSpan) -> None:
    trace = types.SimpleNamespace(get_tracer=lambda name: _FakeTracer(span))
    monkeypatch.setitem(sys.modules, "opentelemetry", types.SimpleNamespace(trace=trace))


def test_step_span_captures_timing() -> None:
    """StepSpan records start/end times and computes duration."""
    with StepSpan(name="test-span") as span:
        pass  # Instant operation
    assert span.duration_ms >= 0
    assert span.end_time > span.start_time


def test_step_span_attributes() -> None:
    """StepSpan stores name, step_id, and custom attributes."""
    span = StepSpan(name="test", step_id="abc-123", attributes={"custom": True})
    assert span.name == "test"
    assert span.step_id == "abc-123"
    assert span.attributes["custom"] is True


def test_step_span_enter_ignores_bad_otel_attributes(monkeypatch) -> None:
    """Bad OTel attributes should not break entering or ending a span."""
    otel_span = _FailingAttributeSpan(fail_keys={"bad"})
    _install_fake_otel(monkeypatch, otel_span)

    with StepSpan(name="test", attributes={"bad": object(), "good": True}):
        pass

    assert otel_span.attributes["good"] is True
    assert otel_span.ended is True


def test_step_span_exit_ends_after_bad_otel_attribute(monkeypatch) -> None:
    """A bad final attribute should not leave the OTel span open."""
    otel_span = _FailingAttributeSpan(fail_keys={"genblaze.duration_ms"})
    _install_fake_otel(monkeypatch, otel_span)

    with StepSpan(name="test", attributes={"good": True}):
        pass

    assert otel_span.attributes["genblaze.retries"] == 0
    assert otel_span.ended is True


def test_structured_logger_emits_json(capfd) -> None:
    """StructuredLogger emits valid JSON to stderr."""
    slog = StructuredLogger("test.observability", level=logging.DEBUG)
    slog.info("test.event", key="value")

    captured = capfd.readouterr()
    record = json.loads(captured.err.strip())
    assert record["event"] == "test.event"
    assert record["key"] == "value"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_structured_logger_with_context(capfd) -> None:
    """with_context adds persistent fields to all log events."""
    slog = StructuredLogger("test.ctx", level=logging.DEBUG)
    child = slog.with_context(run_id="r-123")
    child.info("step.done")

    captured = capfd.readouterr()
    record = json.loads(captured.err.strip())
    assert record["run_id"] == "r-123"


def test_pipeline_structured_log(capfd) -> None:
    """Pipeline with structured_log=True emits JSON events."""
    provider = MockProvider()
    result = Pipeline("obs-test", structured_log=True).step(provider, model="m", prompt="p").run()
    assert result.run.steps[0].status == StepStatus.SUCCEEDED

    captured = capfd.readouterr()
    lines = [line for line in captured.err.strip().split("\n") if line]
    # Filter to JSON lines only (skip stdlib logger plain-text output)
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    event_names = [e["event"] for e in events]
    assert "pipeline.start" in event_names
    assert "step.completed" in event_names
    assert "pipeline.complete" in event_names


def test_provider_invoke_uses_step_span() -> None:
    """Provider.invoke wraps execution in a StepSpan."""
    provider = MockProvider()
    step = Step(provider="mock", model="m", prompt="p")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
