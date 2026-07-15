"""Tests for Pipeline.stream() / astream() and StreamEvent."""

from __future__ import annotations

from typing import Any

import pytest
from genblaze_core.exceptions import PipelineTimeoutError
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
        step.assets.append(
            Asset(url="https://x.test/a.png", media_type="image/png", sha256="0" * 64)
        )
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
        step.assets.append(
            Asset(url="https://x.test/final.png", media_type="image/png", sha256="1" * 64)
        )
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

    Step 2's provider blocks on an Event that this test only releases AFTER
    the assertion — if early break still waited for the full pipeline (the
    old bug), the consumer thread below would never return within its
    bounded join(). This replaces an absolute wall-clock assertion
    (elapsed < 0.5s against 3 x 0.25s sleeps), which could intermittently
    fail on a loaded CI runner since scheduler jitter slows the pipeline's
    own steps too (#48).
    """
    import threading

    release = threading.Event()

    class _BlockingProvider(BaseProvider):
        name = "blocking"

        def submit(self, step, config=None) -> Any:
            release.wait(timeout=5.0)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="2" * 64)
            )
            return step

    pipe = (
        Pipeline("t")
        .step(_OKProvider(), model="m", prompt="p1")
        .step(_BlockingProvider(), model="m", prompt="p2")
    )

    consumer_returned = threading.Event()

    def _consume() -> None:
        for event in pipe.stream():
            if event.type == "step.completed":
                break  # bail after the first step
        consumer_returned.set()

    # Snapshot before spawning so the abandoned "genblaze-stream" worker
    # (joined below, after release) can be found by identity rather than by
    # name — several tests in this module spawn same-named workers.
    pre_existing = {t.ident for t in threading.enumerate()}
    consumer = threading.Thread(target=_consume, daemon=True)
    consumer.start()
    consumer.join(timeout=2.0)

    assert consumer_returned.is_set(), (
        "stream() blocked on early break: consumer did not return while step 2 was in flight"
    )
    release.set()  # let the abandoned background worker wind down

    new_workers = [
        t
        for t in threading.enumerate()
        if t.name == "genblaze-stream" and t.ident not in pre_existing
    ]
    for worker in new_workers:
        worker.join(timeout=5.0)


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
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="3" * 64)
            )
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
    emitter.put() could AttributeError when the generator had moved on.

    Asserts a concrete, deterministic outcome (the daemon thread actually
    winds down) instead of a bare sleep with no assertion — a sleep alone
    gives false coverage of the "daemon winds down cleanly" claim (#48).
    """
    import threading
    import time as _time

    class _SlowProvider(BaseProvider):
        name = "slow"

        def submit(self, step, config=None) -> Any:
            _time.sleep(0.05)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="4" * 64)
            )
            return step

    pipe = (
        Pipeline("t")
        .step(_SlowProvider(), model="m", prompt="p1")
        .step(_SlowProvider(), model="m", prompt="p2")
    )

    # Snapshot before spawning so we can identify THIS test's own worker by
    # identity, not by name — a same-named thread lingering from a previous
    # test (they all share "genblaze-stream") must not be mistaken for ours.
    pre_existing = {t.ident for t in threading.enumerate()}
    for ev in pipe.stream():
        if ev.type == "step.completed":
            break

    # Locate the abandoned daemon worker and wait for it to wind down
    # deterministically instead of guessing with a bare sleep.
    new_workers = [
        t
        for t in threading.enumerate()
        if t.name == "genblaze-stream" and t.ident not in pre_existing
    ]
    assert len(new_workers) == 1, f"expected exactly one new worker thread, found {new_workers}"
    worker = new_workers[0]
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "daemon worker did not wind down after early break"


def test_consecutive_streams_after_early_break_terminate_cleanly() -> None:
    """A second stream() after an early break must still terminate normally
    and deliver its own pipeline.completed.
    """

    class _FastProvider(BaseProvider):
        name = "fast"

        def submit(self, step, config=None) -> Any:
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="5" * 64)
            )
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
        step.assets.append(
            Asset(url="https://x.test/a.png", media_type="image/png", sha256="6" * 64)
        )
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
        step.assets.append(
            Asset(url="https://x.test/final.png", media_type="image/png", sha256="7" * 64)
        )
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
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="8" * 64)
            )
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
        step.assets.append(
            Asset(url="https://x.test/a.png", media_type="image/png", sha256="9" * 64)
        )
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
        step.assets.append(
            Asset(url="https://x.test/a.png", media_type="image/png", sha256="a" * 64)
        )
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
        step.assets.append(
            Asset(url="https://x.test/final.png", media_type="image/png", sha256="b" * 64)
        )
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


# --- Concurrent stream()/astream() isolation (#79, #84) -----------------------


def test_concurrent_streams_on_same_pipeline_do_not_cross_deliver() -> None:
    """Two simultaneous stream() calls on ONE Pipeline instance must not mix
    events between consumers.

    Regression: self._event_emitter used to be a single mutable instance
    attribute, so the second stream()'s install silently clobbered the
    first's, and events cross-delivered between the two consumers.
    """
    import threading

    gate = threading.Event()

    class _GatedProvider(BaseProvider):
        name = "gated"

        def submit(self, step, config=None) -> Any:
            gate.wait(timeout=5.0)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="c" * 64)
            )
            return step

    pipe = Pipeline("t").step(_GatedProvider(), model="m", prompt="p")

    gen_a = pipe.stream()
    gen_b = pipe.stream()

    # Drive both generators up to pipeline.started before releasing the
    # gate, so both workers are genuinely in flight at the same time.
    started_a = next(gen_a)
    started_b = next(gen_b)
    assert started_a.type == "pipeline.started"
    assert started_b.type == "pipeline.started"
    run_a, run_b = started_a.run_id, started_b.run_id
    assert run_a != run_b

    gate.set()  # let both submit() calls proceed concurrently

    events_a = [started_a, *list(gen_a)]
    events_b = [started_b, *list(gen_b)]

    assert all(e.run_id in (None, run_a) for e in events_a), events_a
    assert all(e.run_id in (None, run_b) for e in events_b), events_b
    assert events_a[-1].type == "pipeline.completed"
    assert events_b[-1].type == "pipeline.completed"


@pytest.mark.asyncio
async def test_concurrent_astreams_on_same_pipeline_do_not_cross_deliver() -> None:
    """Async sibling of the sync cross-delivery regression test."""
    import asyncio as _asyncio

    class _GatedAsyncProvider(BaseProvider):
        name = "gated-async"

        def submit(self, step, config=None) -> Any:
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="d" * 64)
            )
            return step

    pipe = Pipeline("t").step(_GatedAsyncProvider(), model="m", prompt="p")

    async def _consume() -> list[StreamEvent]:
        events: list[StreamEvent] = []
        async for ev in pipe.astream():
            events.append(ev)
        return events

    task_a = _asyncio.create_task(_consume())
    task_b = _asyncio.create_task(_consume())
    events_a, events_b = await _asyncio.gather(task_a, task_b)

    run_a = events_a[0].run_id
    run_b = events_b[0].run_id
    assert run_a != run_b
    assert all(e.run_id in (None, run_a) for e in events_a), events_a
    assert all(e.run_id in (None, run_b) for e in events_b), events_b
    assert events_a[-1].type == "pipeline.completed"
    assert events_b[-1].type == "pipeline.completed"


# --- Abandoned-consumer backpressure (#74) -------------------------------------


def test_stream_early_break_stops_terminal_event_after_close() -> None:
    """Early break must close the emitter so the daemon worker's remaining
    events — in particular the terminal event, which only fires after the
    WHOLE pipeline finishes — never land on the abandoned queue.

    Regression: the queue had no maxsize and nothing stopped the abandoned
    worker from enqueuing events for the full remaining run length. This
    also guards a fix-interaction: once the emitter moved to a per-thread
    ContextVar slot (#79/#84), the consumer thread can no longer reach into
    the worker thread's slot to implicitly cut it off — an explicit
    ``emitter.close()`` on early break is what stops the worker now.
    """
    import queue as _queue
    import threading
    import time as _time
    from unittest.mock import patch

    captured: list[object] = []
    real_queue_cls = _queue.Queue

    class _SpyQueue(real_queue_cls):
        def put(self, item, *a, **kw):
            captured.append(item)
            return super().put(item, *a, **kw)

    class _SlowProvider(BaseProvider):
        name = "slow"

        def submit(self, step, config=None) -> Any:
            _time.sleep(0.1)
            return "pred"

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step):
            step.assets.append(
                Asset(url="https://x.test/a.png", media_type="image/png", sha256="e" * 64)
            )
            return step

    pipe = (
        Pipeline("t")
        .step(_SlowProvider(), model="m", prompt="p1")
        .step(_SlowProvider(), model="m", prompt="p2")
        .step(_SlowProvider(), model="m", prompt="p3")
    )

    with patch("queue.Queue", _SpyQueue):
        # Snapshot before spawning so we can identify THIS test's own
        # worker by identity, not by name — a same-named thread lingering
        # from a previous test (which shares "genblaze-stream") must not be
        # mistaken for ours.
        pre_existing = {t.ident for t in threading.enumerate()}
        for ev in pipe.stream():
            if ev.type == "step.completed":
                break

        new_workers = [
            t
            for t in threading.enumerate()
            if t.name == "genblaze-stream" and t.ident not in pre_existing
        ]
        assert len(new_workers) == 1, (
            f"expected exactly one new worker thread, found {new_workers}"
        )
        worker = new_workers[0]
        worker.join(timeout=5.0)
        assert not worker.is_alive()

    # Guard against the queue.Queue patch silently failing to intercept
    # (e.g. a future import-style change in stream()) and giving false
    # confidence that the terminal-event assertion below actually ran.
    assert captured, "spy did not capture any queue.Queue.put calls"

    types = [getattr(item, "type", "<sentinel>") for item in captured]
    assert "pipeline.completed" not in types, (
        f"abandoned worker ran the full pipeline and emitted a terminal "
        f"event onto a queue nobody drains: {types}"
    )


# --- Aborted-run terminal event correctness (#85) ------------------------------


def test_stream_timeout_before_any_step_emits_failed_not_completed() -> None:
    """A pipeline_timeout=0 exit before any step runs must emit
    pipeline.failed, never pipeline.completed.

    Regression: the run()/arun() ``finally`` fallback called ``_finalize``
    with an empty completed_steps list, and ``all([])`` is True, so the
    aborted run was reported COMPLETED.
    """
    events: list[StreamEvent] = []
    with pytest.raises(PipelineTimeoutError):
        for ev in (
            Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream(pipeline_timeout=0)
        ):
            events.append(ev)
    types = _event_types(events)
    assert "pipeline.completed" not in types
    assert types[-1] == "pipeline.failed"


@pytest.mark.asyncio
async def test_astream_timeout_before_any_step_emits_failed_not_completed() -> None:
    """Async sibling of the sync timeout-before-any-step regression test."""
    events: list[StreamEvent] = []
    with pytest.raises(PipelineTimeoutError):
        async for ev in (
            Pipeline("t").step(_OKProvider(), model="m", prompt="p").astream(pipeline_timeout=0)
        ):
            events.append(ev)
    types = _event_types(events)
    assert "pipeline.completed" not in types
    assert types[-1] == "pipeline.failed"


@pytest.mark.asyncio
async def test_astream_concurrent_timeout_fires_before_step_started() -> None:
    """Concurrent (non-chained) async pipelines must not emit step.started
    for steps that will never run when pipeline_timeout=0 fires immediately.
    """
    events: list[StreamEvent] = []
    with pytest.raises(PipelineTimeoutError):
        async for ev in (
            Pipeline("t")
            .step(_OKProvider(), model="m", prompt="p1")
            .step(_OKProvider(), model="m", prompt="p2")
            .astream(pipeline_timeout=0)
        ):
            events.append(ev)
    types = _event_types(events)
    assert "step.started" not in types
    assert "pipeline.completed" not in types
    assert types[-1] == "pipeline.failed"


# --- Fail-fast cancellation preserves step_id (#86) ----------------------------


class _FastFailProvider(BaseProvider):
    """Fails synchronously in submit() — no delay."""

    name = "fast-fail"

    def submit(self, step, config=None) -> Any:
        raise RuntimeError("boom")

    def poll(self, prediction_id, config=None) -> bool:  # pragma: no cover
        return True

    def fetch_output(self, prediction_id, step):  # pragma: no cover
        return step


class _SlowVictimProvider(BaseProvider):
    """Slow enough that fail-fast cancels it before it finishes polling."""

    name = "slow-victim"

    def submit(self, step, config=None) -> Any:
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:
        import time as _time

        _time.sleep(0.2)  # gives the sibling time to fail first
        return True

    def fetch_output(self, prediction_id, step):
        step.assets.append(
            Asset(url="https://x.test/a.png", media_type="image/png", sha256="f" * 64)
        )
        return step


@pytest.mark.asyncio
async def test_concurrent_fail_fast_cancellation_preserves_step_id() -> None:
    """A cancelled sibling step's step.failed event must carry the SAME
    step_id as its own earlier step.started event.

    Regression: the cancellation placeholder was built via a fresh
    ``_build_step()`` call with no step_id, minting a brand-new UUID that
    didn't match the step_id already announced via step.started.
    """
    events: list[StreamEvent] = []
    async for ev in (
        Pipeline("t")
        .step(_FastFailProvider(), model="m", prompt="p1")
        .step(_SlowVictimProvider(), model="m", prompt="p2")
        .astream()
    ):
        events.append(ev)

    started_ids = {e.step_id for e in events if e.type == "step.started"}
    failed = [e for e in events if e.type == "step.failed"]
    assert len(failed) == 2  # one raised in submit(), one cancelled mid-poll
    for f in failed:
        assert f.step_id in started_ids, (
            f"step.failed step_id {f.step_id} has no matching step.started ({started_ids})"
        )


class _RaisingAinvokeProvider(BaseProvider):
    """Raises directly from ainvoke() — exercises the task-exception path
    in _gather_fail_fast (as opposed to a provider returning a FAILED step)."""

    name = "raising-ainvoke"

    async def ainvoke(self, step, config=None):
        raise RuntimeError("ainvoke exploded")

    def submit(self, step, config=None) -> Any:  # pragma: no cover
        return "pred"

    def poll(self, prediction_id, config=None) -> bool:  # pragma: no cover
        return True

    def fetch_output(self, prediction_id, step):  # pragma: no cover
        return step


@pytest.mark.asyncio
async def test_concurrent_fail_fast_task_exception_preserves_step_id() -> None:
    """A task that raises (not just returns a FAILED step) must still emit
    step.failed under its own already-announced step_id.
    """
    events: list[StreamEvent] = []
    async for ev in (
        Pipeline("t")
        .step(_RaisingAinvokeProvider(), model="m", prompt="p1")
        .step(_SlowVictimProvider(), model="m", prompt="p2")
        .astream()
    ):
        events.append(ev)

    started_ids = {e.step_id for e in events if e.type == "step.started"}
    failed = [e for e in events if e.type == "step.failed"]
    assert failed, "expected at least one step.failed event"
    for f in failed:
        assert f.step_id in started_ids, (
            f"step.failed step_id {f.step_id} has no matching step.started ({started_ids})"
        )


# --- step.completed / step.failed carry run_id (#87) ---------------------------


def test_step_completed_event_carries_run_id() -> None:
    events = list(Pipeline("t").step(_OKProvider(), model="m", prompt="p").stream())
    run_id = events[0].run_id
    completed = next(e for e in events if e.type == "step.completed")
    assert completed.run_id == run_id


def test_step_failed_event_carries_run_id() -> None:
    events = list(Pipeline("t").step(_FailProvider(), model="m", prompt="p").stream())
    run_id = events[0].run_id
    failed = next(e for e in events if e.type == "step.failed")
    assert failed.run_id == run_id


@pytest.mark.asyncio
async def test_astream_step_completed_event_carries_run_id() -> None:
    events: list[StreamEvent] = []
    async for ev in Pipeline("t").step(_OKProvider(), model="m", prompt="p").astream():
        events.append(ev)
    run_id = events[0].run_id
    completed = next(e for e in events if e.type == "step.completed")
    assert completed.run_id == run_id
