"""Tests for observability — StepSpan and StructuredLogger integration."""

from __future__ import annotations

import json
import logging
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
