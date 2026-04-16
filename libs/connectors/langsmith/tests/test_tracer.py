"""Tests for LangSmithTracer — verifies lifecycle events route to the client."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider


class _MockProvider(BaseProvider):
    name = "mock"

    def submit(self, step, config=None) -> Any:
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://example.com/a.png", media_type="image/png"))
        return step


def _make_tracer() -> tuple[Any, MagicMock]:
    from genblaze_langsmith.tracer import LangSmithTracer

    mock_client = MagicMock()
    tracer = LangSmithTracer(project_name="test-proj", client=mock_client)
    return tracer, mock_client


def test_pipeline_emits_run_and_step_events() -> None:
    tracer, client = _make_tracer()

    result = Pipeline("ls-test", tracer=tracer).step(_MockProvider(), model="m", prompt="p").run()

    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    # One run-level + one step-level create_run call
    assert client.create_run.call_count == 2
    # Two update_run calls (step end + run end)
    assert client.update_run.call_count == 2

    # Check project routing
    for call in client.create_run.call_args_list:
        assert call.kwargs["project_name"] == "test-proj"


def test_langsmith_errors_do_not_break_pipeline() -> None:
    """Tracer backend exceptions must be swallowed by the pipeline."""
    tracer, client = _make_tracer()
    client.create_run.side_effect = RuntimeError("langsmith unreachable")

    result = Pipeline("ls-err", tracer=tracer).step(_MockProvider(), model="m", prompt="p").run()
    # Pipeline still completes successfully despite tracer failures
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


def test_import_guard_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instantiation without SDK + without explicit client raises ImportError."""
    import sys

    from genblaze_langsmith.tracer import LangSmithTracer

    # Force import failure by evicting langsmith from sys.modules
    monkeypatch.setitem(sys.modules, "langsmith", None)
    with pytest.raises(ImportError, match="genblaze-langsmith"):
        LangSmithTracer()
