"""Tests for ProgressEvent and progress callback wiring."""

from __future__ import annotations

from genblaze_core.providers.progress import ProgressEvent


def test_progress_event_construction():
    """ProgressEvent can be created with all fields."""
    event = ProgressEvent(
        step_id="step-123",
        provider="runway",
        model="gen4_turbo",
        status="processing",
        progress_pct=0.5,
        elapsed_sec=3.2,
        message="Generating video...",
    )
    assert event.step_id == "step-123"
    assert event.provider == "runway"
    assert event.model == "gen4_turbo"
    assert event.status == "processing"
    assert event.progress_pct == 0.5
    assert event.elapsed_sec == 3.2
    assert event.message == "Generating video..."


def test_progress_event_defaults():
    """message defaults to None, progress_pct can be None."""
    event = ProgressEvent(
        step_id="s",
        provider="p",
        model="m",
        status="submitted",
        progress_pct=None,
        elapsed_sec=0.0,
    )
    assert event.message is None
    assert event.progress_pct is None


def test_progress_event_exportable():
    """ProgressEvent is accessible from the top-level package."""
    from genblaze_core import ProgressEvent as PE

    assert PE is ProgressEvent
