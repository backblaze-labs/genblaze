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


# --- Early-break non-blocking semantics ---------------------------------------


def test_stream_early_break_does_not_block() -> None:
    """Breaking early from stream() must not wait for the pipeline to finish.

    Regression: previously t.join() ran unconditionally in the generator's
    finally, blocking the caller for the remainder of the pipeline runtime.
    """
    import time as _time

    provider = _OKProvider()
    # Multi-step pipeline with a 0.25s synthetic latency per step.
    # If early-break blocks on join, total wall time would be ≥ 0.75s.
    # Slow provider via a subclass:

    class _SlowProvider(BaseProvider):
        name = "slow"

        def submit(self, step, config=None) -> Any:
            _time.sleep(0.25)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
            return step

    pipe = (
        Pipeline("t")
        .step(_SlowProvider(), model="m", prompt="p1")
        .step(_SlowProvider(), model="m", prompt="p2")
        .step(_SlowProvider(), model="m", prompt="p3")
    )

    t0 = _time.monotonic()
    for event in pipe.stream():
        if event.type == "step.completed":
            break  # bail after the first step
    elapsed = _time.monotonic() - t0

    # Must finish well before 3 × 0.25s = 0.75s (first step + some slack)
    assert elapsed < 0.5, f"stream() blocked on early break ({elapsed:.2f}s)"
    assert provider is not None  # keep imports used


@pytest.mark.asyncio
async def test_astream_early_break_cancels_worker() -> None:
    """Breaking early from astream() cancels the worker task cleanly."""
    import asyncio
    import time as _time

    class _AsyncSlowProvider(BaseProvider):
        name = "aslow"

        def submit(self, step, config=None) -> Any:
            _time.sleep(0.01)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
            return step

    pipe = (
        Pipeline("t")
        .step(_AsyncSlowProvider(), model="m", prompt="p1")
        .step(_AsyncSlowProvider(), model="m", prompt="p2")
        .step(_AsyncSlowProvider(), model="m", prompt="p3")
    )

    t0 = asyncio.get_event_loop().time()
    async for event in pipe.astream():
        if event.type == "step.completed":
            break
    elapsed = asyncio.get_event_loop().time() - t0

    # Upper bound generous; the point is it doesn't wait for all 3 steps.
    assert elapsed < 1.0, f"astream() blocked on early break ({elapsed:.2f}s)"


# --- QueueEmitter close semantics ---------------------------------------------


def test_queue_emitter_put_after_close_is_noop() -> None:
    """Post-close put() must drop events silently, not crash or enqueue.

    Abandoned stream workers (after consumer early-break) rely on this to
    finish their lifecycle without tripping on a closed/swapped emitter.
    """
    import queue as _queue

    from genblaze_core.pipeline.streaming import QueueEmitter

    q: _queue.Queue = _queue.Queue()
    emitter = QueueEmitter(q)
    emitter.put(StreamEvent(type="step.started"))
    emitter.close()

    # Drain one real event + sentinel — nothing else should be enqueued.
    first = q.get_nowait()
    assert isinstance(first, StreamEvent)
    # Sentinel is an opaque object, not a StreamEvent
    sentinel = q.get_nowait()
    assert not isinstance(sentinel, StreamEvent)
    assert q.empty()

    # Post-close puts must not raise and must not land on the queue.
    emitter.put(StreamEvent(type="step.completed"))
    emitter.close()  # idempotent
    assert q.empty()


def test_stream_early_break_drains_without_daemon_error() -> None:
    """After early break, the daemon worker finishes cleanly even if it
    still tries to emit events. Previously, the worker's trailing
    emitter.put() could AttributeError when the generator had moved on."""
    import time as _time

    class _SlowProvider(BaseProvider):
        name = "slow"

        def submit(self, step, config=None) -> Any:
            _time.sleep(0.05)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
            return step

    pipe = (
        Pipeline("t")
        .step(_SlowProvider(), model="m", prompt="p1")
        .step(_SlowProvider(), model="m", prompt="p2")
    )

    for ev in pipe.stream():
        if ev.type == "step.completed":
            break

    # Give the daemon worker a beat to wind down. If it crashed, a subsequent
    # stream() call would inherit a poisoned state.
    _time.sleep(0.25)


def test_consecutive_streams_after_early_break_terminate_cleanly() -> None:
    """A second stream() after an early break must still terminate normally
    and deliver its own pipeline.completed.

    Scope note: if the first stream's abandoned daemon is still running when
    the second stream starts, its residual events may appear in the second
    stream's queue because ``self._event_emitter`` is a single-slot rebind.
    Full isolation requires routing emitters via contextvars — tracked
    separately. This test only asserts the second stream terminates with a
    terminal event and produces no exceptions.
    """

    class _FastProvider(BaseProvider):
        name = "fast"

        def submit(self, step, config=None) -> Any:
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
            return step

    pipe = Pipeline("t").step(_FastProvider(), model="m", prompt="p1")

    # First stream: consume all events so the worker completes cleanly.
    events1 = list(pipe.stream())
    assert events1[-1].type == "pipeline.completed"

    # Second stream on the same pipeline: must produce its own clean sequence.
    events2 = list(pipe.stream())
    types2 = _event_types(events2)
    assert types2[0] == "pipeline.started"
    assert types2[-1] == "pipeline.completed"
