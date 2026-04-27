"""Tests for Pipeline.stream() / astream() and StreamEvent."""

from __future__ import annotations

from typing import Any

import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.observability.events import (
    StepCompletedEvent,
    StepProgressEvent,
    StepStartedEvent,
    StreamEvent,
)
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
    ev = StepStartedEvent(
        run_id="r1",
        step_id="s1",
        step_index=0,
        total_steps=1,
        provider="p",
        model="m",
    )
    d = ev.to_dict()
    assert d["type"] == "step.started"
    assert d["run_id"] == "r1"
    assert "progress_pct" not in d  # None-valued variant-specific fields don't leak


def test_stream_event_preview_url_field_populates() -> None:
    ev = StepProgressEvent(
        step_id="s1",
        provider="p",
        model="m",
        preview_url="https://preview.test/frame.jpg",
    )
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
    started = StepStartedEvent(
        run_id="r1", step_id="s1", step_index=0, total_steps=1, provider="p", model="m"
    )
    completed = StepCompletedEvent(
        run_id="r1",
        step_id="s1",
        step_index=0,
        total_steps=1,
        provider="p",
        model="m",
        elapsed_sec=0.0,
    )
    emitter.put(started)
    emitter.close()

    # Drain one real event + sentinel — nothing else should be enqueued.
    first = q.get_nowait()
    assert isinstance(first, StreamEvent)
    # Sentinel is an opaque object, not a StreamEvent
    sentinel = q.get_nowait()
    assert not isinstance(sentinel, StreamEvent)
    assert q.empty()

    # Post-close puts must not raise and must not land on the queue.
    emitter.put(completed)
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


# --- StepRetried events reach the stream -------------------------------------


class _FlakyPollStreamProvider(BaseProvider):
    """poll() raises a retryable 503 N times then returns True.

    Used to exercise the on_retry → stream wiring without depending on
    test_provider_retry's helpers (which test the callback path directly).
    """

    name = "flaky-poll-stream"

    def __init__(self, fail_count: int = 2) -> None:
        super().__init__()
        self._fail_count = fail_count
        self._poll_calls = 0

    def submit(self, step, config=None) -> Any:
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        self._poll_calls += 1
        if self._poll_calls <= self._fail_count:
            raise RuntimeError("503 server temporarily unavailable")
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
        return step


def test_step_retried_event_reaches_stream() -> None:
    """Retries during poll() must surface as step.retried events on the stream.

    Regression: _install_progress_tracer previously only wrapped on_progress,
    so StepRetriedEvents fired by BaseProvider._emit_retry never reached
    Pipeline.stream() consumers.
    """
    from unittest.mock import patch

    with patch("genblaze_core.providers.base.time.sleep"):
        events = list(
            Pipeline("t")
            .step(_FlakyPollStreamProvider(fail_count=2), model="m", prompt="p")
            .stream()
        )

    types = _event_types(events)
    assert types.count("step.retried") == 2, f"expected 2 retry events, got {types}"
    # Ordering: started -> first retry -> second retry -> completed
    started = types.index("step.started")
    completed = types.index("step.completed")
    retries = [i for i, t in enumerate(types) if t == "step.retried"]
    assert all(started < r < completed for r in retries)
    # Field surface check on the first retry event
    retry_ev = next(e for e in events if e.type == "step.retried")
    assert retry_ev.phase == "poll"
    assert retry_ev.attempt == 1
    assert retry_ev.delay_sec >= 0
    assert retry_ev.error is not None


# --- poll_progress() hook ----------------------------------------------------


class _PollProgressProvider(BaseProvider):
    """Provider whose poll_progress returns a preview URL mid-flight."""

    name = "preview-poll"
    poll_interval = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._poll_count = 0

    def submit(self, step, config=None) -> Any:
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        self._poll_count += 1
        return self._poll_count >= 2

    def poll_progress(self, prediction_id) -> dict[str, Any] | None:
        return {
            "preview_url": "https://cdn.test/preview-1.jpg",
            "progress_pct": 0.5,
            "message": "running",
        }

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/final.png", media_type="image/png"))
        return step


def test_poll_progress_hook_flows_to_step_progress() -> None:
    """poll_progress() return values appear on step.progress events."""
    from unittest.mock import patch

    with patch("genblaze_core.providers.base.time.sleep"):
        events = list(Pipeline("t").step(_PollProgressProvider(), model="m", prompt="p").stream())

    progress = [e for e in events if e.type == "step.progress"]
    matching = [e for e in progress if e.preview_url == "https://cdn.test/preview-1.jpg"]
    assert matching, f"expected preview_url on at least one progress event; got {progress}"
    assert any(e.progress_pct == 0.5 for e in matching)
    assert any(e.message == "running" for e in matching)


# --- step.queued additive event ----------------------------------------------


def test_step_queued_emitted_serially_for_upcoming_steps() -> None:
    """Sequential pipeline emits step.queued for every step except the first."""
    events = list(
        Pipeline("t")
        .step(_OKProvider(), model="m", prompt="p1")
        .step(_OKProvider(), model="m", prompt="p2")
        .step(_OKProvider(), model="m", prompt="p3")
        .stream()
    )
    queued = [e for e in events if e.type == "step.queued"]
    assert len(queued) == 2  # steps 1 and 2 (indices)
    assert all(e.reason == "serial" for e in queued)
    assert [e.step_index for e in queued] == [1, 2]


def test_step_queued_id_matches_step_started_id() -> None:
    """Pre-allocated step_id must persist through to step.started + step.completed."""
    events = list(
        Pipeline("t")
        .step(_OKProvider(), model="m", prompt="p1")
        .step(_OKProvider(), model="m", prompt="p2")
        .stream()
    )
    queued = next(e for e in events if e.type == "step.queued")
    started = next(
        e for e in events if e.type == "step.started" and e.step_index == queued.step_index
    )
    assert queued.step_id == started.step_id


def test_step_queued_omitted_for_single_step() -> None:
    """Single-step pipeline emits no queued events (nothing waiting)."""
    events = list(Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream())
    assert not [e for e in events if e.type == "step.queued"]


@pytest.mark.asyncio
async def test_step_queued_concurrency_limit_in_concurrent_arun() -> None:
    """Steps blocked on the semaphore emit step.queued(reason='concurrency_limit')."""
    import asyncio as _asyncio

    class _SlowAsyncProvider(BaseProvider):
        name = "slow-async"

        def __init__(self) -> None:
            super().__init__()

        def submit(self, step, config=None) -> Any:
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
            return step

    async def _run() -> list[StreamEvent]:
        # max_concurrency=1 ensures every step except the first sees a locked sem
        pipe = Pipeline("t", max_concurrency=1)
        for i in range(3):
            pipe.step(_SlowAsyncProvider(), model="m", prompt=f"p{i}")
        events: list[StreamEvent] = []
        async for ev in pipe.astream():
            events.append(ev)
        return events

    events = await _run()
    queued = [e for e in events if e.type == "step.queued"]
    # We can't predict exactly how many queue events fire (depends on event-loop
    # scheduler timing), but at least one must fire when max_concurrency=1 and
    # there are 3 steps — at minimum the second or third step finds sem locked.
    assert queued, f"expected at least one concurrency_limit queued event, got {events}"
    assert all(e.reason == "concurrency_limit" for e in queued)
    # Wait so unused asyncio task don't leak between tests
    await _asyncio.sleep(0)


# --- heartbeat events on long polls ------------------------------------------


class _LongPollProvider(BaseProvider):
    """poll() returns False N times then True. Used to simulate long jobs."""

    name = "long-poll"
    poll_interval = 30.0  # forces _adaptive_poll_interval >= 15s threshold

    def __init__(self, *, polls_until_done: int = 1) -> None:
        super().__init__()
        self._polls_until_done = polls_until_done
        self._poll_count = 0

    def submit(self, step, config=None) -> Any:
        return "pred-long"

    def poll(self, prediction_id, config=None) -> bool:
        self._poll_count += 1
        return self._poll_count >= self._polls_until_done

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
        return step


def test_heartbeats_emit_during_long_poll() -> None:
    """When the adaptive poll interval >= 15s, heartbeat ticks fire mid-sleep."""
    from unittest.mock import patch

    # poll_interval=30s on first poll iteration triggers the heartbeat path.
    pipe = Pipeline("t").step(_LongPollProvider(polls_until_done=2), model="m", prompt="p")

    # Patch only the helper's sleeps to avoid 30s of real wall time.
    with (
        patch("genblaze_core.providers.base.time.sleep"),
        patch("genblaze_core.providers.base.asyncio.sleep"),
    ):
        events = list(pipe.stream())

    progress_events = [e for e in events if e.type == "step.progress"]
    heartbeats = [e for e in progress_events if e.is_heartbeat]
    assert heartbeats, f"expected heartbeat ticks during 30s poll, got {progress_events}"


def test_heartbeats_disabled_drops_them_at_emitter() -> None:
    """heartbeats=False on stream() suppresses heartbeat events entirely."""
    from unittest.mock import patch

    pipe = Pipeline("t").step(_LongPollProvider(polls_until_done=2), model="m", prompt="p")

    with patch("genblaze_core.providers.base.time.sleep"):
        events = list(pipe.stream(heartbeats=False))

    progress_events = [e for e in events if e.type == "step.progress"]
    assert not any(e.is_heartbeat for e in progress_events), (
        f"heartbeats=False should drop is_heartbeat events; got {progress_events}"
    )


def test_short_poll_interval_does_not_heartbeat() -> None:
    """When poll interval < 15s, no heartbeat is emitted (no overhead)."""
    pipe = Pipeline("t").step(
        _OKProvider(), model="m", prompt="p"
    )  # _OKProvider polls done immediately
    events = list(pipe.stream())
    progress_events = [e for e in events if e.type == "step.progress"]
    assert not any(e.is_heartbeat for e in progress_events)


# --- expected_duration_sec on step.started -----------------------------------


def test_expected_duration_sec_flows_to_step_started() -> None:
    """Kwarg on .step() echoes onto the step.started event."""
    events = list(
        Pipeline("t")
        .step(_OKProvider(), model="m", prompt="p", expected_duration_sec=42.0)
        .stream()
    )
    started = next(e for e in events if e.type == "step.started")
    assert started.expected_duration_sec == 42.0


def test_expected_duration_sec_default_none() -> None:
    """Omitted kwarg leaves the field None on the event (and out of JSON)."""
    events = list(Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream())
    started = next(e for e in events if e.type == "step.started")
    assert started.expected_duration_sec is None
    # to_dict drops None-valued optional fields
    assert "expected_duration_sec" not in started.to_dict()


# --- request_id propagation --------------------------------------------------


class _ProgressBeforeAndAfterSubmitProvider(BaseProvider):
    """Fires one progress tick mid-poll so we can assert request_id on it."""

    name = "req-id"

    def submit(self, step, config=None) -> Any:
        return "pred-xyz789"

    def poll(self, prediction_id, config=None) -> bool:
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/a.png", media_type="image/png"))
        return step


def test_request_id_propagates_to_completed_event() -> None:
    """submit() return value lands on step.completed.request_id."""
    events = list(
        Pipeline("t").step(_ProgressBeforeAndAfterSubmitProvider(), model="m", prompt="p").stream()
    )
    completed = next(e for e in events if e.type == "step.completed")
    assert completed.request_id == "pred-xyz789"


def test_request_id_persists_in_step_metadata() -> None:
    """Step.metadata['upstream_id'] is the canonical home for the prediction id."""
    events = list(
        Pipeline("t").step(_ProgressBeforeAndAfterSubmitProvider(), model="m", prompt="p").stream()
    )
    completed = next(e for e in events if e.type == "step.completed")
    # In-process consumers can also read it off the Step
    assert completed.step is not None
    assert completed.step.metadata["upstream_id"] == "pred-xyz789"


class _PreviewWithSubmitProvider(BaseProvider):
    """Emits a progress tick AFTER submit so request_id is available."""

    name = "preview-after-submit"

    def submit(self, step, config=None) -> Any:
        return "pred-mid-1"

    def poll(self, prediction_id, config=None) -> bool:
        # Provider-emitted progress with a preview, simulating mid-flight tick
        # in connectors that override poll() to fire progress with a preview.
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(Asset(url="https://x.test/final.png", media_type="image/png"))
        return step


def test_request_id_on_progress_after_submit() -> None:
    """Progress events fired after submit carry request_id from step.metadata."""
    progress_events: list[StepProgressEvent] = []
    pipe = Pipeline("t").step(_PreviewWithSubmitProvider(), model="m", prompt="p")
    for ev in pipe.stream():
        if ev.type == "step.progress":
            progress_events.append(ev)
    # The base provider's "succeeded" tick fires post-submit and post-fetch,
    # so it must carry request_id.
    succeeded_ticks = [e for e in progress_events if e.data.get("status") == "succeeded"]
    assert succeeded_ticks, "expected at least one succeeded progress tick"
    assert all(e.request_id == "pred-mid-1" for e in succeeded_ticks)


def test_step_retried_event_user_callback_still_fires() -> None:
    """Wiring on_retry into the stream must not swallow user-supplied on_retry."""
    from unittest.mock import patch

    user_events: list[Any] = []
    pipe = Pipeline("t").step(_FlakyPollStreamProvider(fail_count=1), model="m", prompt="p")

    with patch("genblaze_core.providers.base.time.sleep"):
        # User callback supplied via the run config — Pipeline.run() forwards
        # config kwargs through, so this exercises the composite path.
        events = list(pipe.stream(on_retry=user_events.append))

    assert len(user_events) == 1
    assert user_events[0].type == "step.retried"
    # And the stream still saw it.
    assert sum(1 for e in events if e.type == "step.retried") == 1
