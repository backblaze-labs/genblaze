"""Tests for Pipeline.stream() / astream() and StreamEvent."""

from __future__ import annotations

from typing import Any

import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.observability.events import StreamEvent
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider
from genblaze_core.providers.progress import ProgressEvent


class _OKProvider(BaseProvider):
    name = "mock"

    def submit(self, step, config=None) -> Any:
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
        return step


class _FailProvider(BaseProvider):
    name = "fail"

    def submit(self, step, config=None):
        raise RuntimeError("boom")

    def poll(self, prediction_id, config=None) -> bool:  # pragma: no cover
        return True

    def fetch_output(self, prediction_id, step):  # pragma: no cover
        return step


def _event_types(events: list[StreamEvent]) -> list[str]:
    return [e.type for e in events]


# --- Event model --------------------------------------------------------------


def test_stream_event_to_dict_trims_nulls() -> None:
    ev = StreamEvent(type="step.started", run_id="r1", step_id="s1")
    d = ev.to_dict()
    assert d["type"] == "step.started"
    assert d["run_id"] == "r1"
    assert "progress_pct" not in d  # None fields dropped


def test_stream_event_preview_url_field_populates() -> None:
    ev = StreamEvent(type="step.progress", preview_url="https://preview.test/frame.jpg")
    assert ev.preview_url == "https://preview.test/frame.jpg"
    assert ev.to_dict()["preview_url"] == "https://preview.test/frame.jpg"


def test_progress_event_preview_url_field() -> None:
    """ProgressEvent gained a preview_url field — default None, opt-in for providers."""
    ev = ProgressEvent(
        step_id="s",
        provider="p",
        model="m",
        status="processing",
        progress_pct=0.5,
        elapsed_sec=1.0,
        preview_url="https://cdn.test/frame-42.jpg",
    )
    assert ev.preview_url == "https://cdn.test/frame-42.jpg"


# --- stream() sync ------------------------------------------------------------


def test_stream_yields_ordered_events() -> None:
    events = list(Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream())
    types = _event_types(events)
    assert types[0] == "pipeline.started"
    assert "step.started" in types
    assert "step.completed" in types
    assert types[-1] == "pipeline.completed"
    # Final event carries the full result
    assert events[-1].result is not None
    assert events[-1].result.run.steps[0].status == StepStatus.SUCCEEDED


def test_stream_failure_emits_pipeline_failed() -> None:
    events = list(Pipeline("t").step(_FailProvider(), model="m", prompt="p").stream())
    types = _event_types(events)
    assert types[0] == "pipeline.started"
    assert "step.failed" in types
    assert types[-1] == "pipeline.failed"


def test_stream_terminal_event_has_result() -> None:
    events = list(Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream())
    final = events[-1]
    assert final.type == "pipeline.completed"
    assert final.result is not None
    assert final.run_id is not None
    assert final.result.manifest.verify()


def test_stream_run_kwargs_pass_through(tmp_path) -> None:
    """stream() forwards kwargs like timeout to run()."""
    # Should not raise even when timeout passed
    events = list(Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream(timeout=10.0))
    assert events[-1].type == "pipeline.completed"


# --- astream() async ----------------------------------------------------------


@pytest.mark.asyncio
async def test_astream_yields_ordered_events() -> None:
    events: list[StreamEvent] = []
    async for ev in Pipeline("t").step(_OKProvider(), model="m", prompt="p").astream():
        events.append(ev)
    types = _event_types(events)
    assert types[0] == "pipeline.started"
    assert types[-1] == "pipeline.completed"


@pytest.mark.asyncio
async def test_astream_failure_emits_pipeline_failed() -> None:
    events: list[StreamEvent] = []
    async for ev in Pipeline("t").step(_FailProvider(), model="m", prompt="p").astream():
        events.append(ev)
    types = _event_types(events)
    assert "step.failed" in types
    assert types[-1] == "pipeline.failed"


# --- preview URL --------------------------------------------------------------


class _PreviewProvider(BaseProvider):
    """Provider that emits a preview URL mid-generation via on_progress."""

    name = "preview"

    def submit(self, step, config=None) -> Any:
        # Fire one progress tick with a preview_url before returning
        self._fire_progress(
            step,
            config,
            status="processing",
            start_time=0.0,
            progress_pct=0.5,
            preview_url="https://cdn.test/preview-42.jpg",
        )
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/final.png", media_type="image/png"))
        return step


def test_preview_url_surfaces_in_stream() -> None:
    events = list(Pipeline("t").step(_PreviewProvider(), model="m", prompt="p").stream())
    progress_events = [e for e in events if e.type == "step.progress"]
    assert any(e.preview_url == "https://cdn.test/preview-42.jpg" for e in progress_events)
