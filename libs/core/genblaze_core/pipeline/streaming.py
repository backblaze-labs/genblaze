"""Streaming helpers — bridge pipeline callbacks into push-style event queues.

Used by :meth:`Pipeline.stream`, :meth:`Pipeline.astream`, and the agent
loop streams to emit a single ordered stream of :class:`StreamEvent`
instances. The emitter wraps the existing ``on_progress``, ``on_retry``,
and ``on_step_complete`` callbacks so no provider changes are required.
"""

from __future__ import annotations

import asyncio
import contextvars
import queue
from typing import TYPE_CHECKING

from genblaze_core.models.step import UPSTREAM_ID_KEY
from genblaze_core.observability.events import (
    StepCompletedEvent,
    StepFailedEvent,
    StepProgressEvent,
    StepQueuedEvent,
    StepRetriedEvent,
    StreamEvent,
)

if TYPE_CHECKING:
    from genblaze_core.pipeline.result import StepCompleteEvent
    from genblaze_core.providers.progress import ProgressEvent


_SENTINEL = object()


def progress_to_stream_event(ev: ProgressEvent, run_id: str | None = None) -> StepProgressEvent:
    """Map a provider ProgressEvent to a StepProgressEvent.

    Single source of truth for this translation — used by both the
    queue-backed emitter and Pipeline's in-process progress fan-out.
    """
    return StepProgressEvent(
        run_id=run_id,
        step_id=ev.step_id,
        provider=ev.provider,
        model=ev.model,
        request_id=ev.request_id,
        progress_pct=ev.progress_pct,
        elapsed_sec=ev.elapsed_sec,
        preview_url=ev.preview_url,
        message=ev.message,
        is_heartbeat=ev.is_heartbeat,
        data={"status": ev.status},
    )


def step_complete_to_stream_event(ev: StepCompleteEvent, run_id: str | None = None) -> StreamEvent:
    """Map a pipeline StepCompleteEvent to the matching variant."""
    from genblaze_core.models.enums import StepStatus

    failed = ev.step.status == StepStatus.FAILED
    step_status = str(ev.step.status)
    # Mirror the upstream prediction id onto the wire-format event so
    # consumers reading the JSON surface (no in-process Step) still see it.
    request_id = ev.step.metadata.get(UPSTREAM_ID_KEY)
    if failed:
        return StepFailedEvent(
            run_id=run_id,
            step_id=ev.step.step_id,
            step_index=ev.step_index,
            total_steps=ev.total_steps,
            provider=ev.step.provider or "",
            model=ev.step.model,
            request_id=request_id,
            elapsed_sec=ev.elapsed_sec,
            step=ev.step,
            step_status=step_status,
            error=ev.step.error,
        )
    return StepCompletedEvent(
        run_id=run_id,
        step_id=ev.step.step_id,
        step_index=ev.step_index,
        total_steps=ev.total_steps,
        provider=ev.step.provider or "",
        model=ev.step.model,
        request_id=request_id,
        elapsed_sec=ev.elapsed_sec,
        step=ev.step,
        step_status=step_status,
    )


class QueueEmitter:
    """Pushes StreamEvents onto a queue — works with both queue types.

    Accepts either a ``queue.Queue`` (for :meth:`Pipeline.stream`) or an
    ``asyncio.Queue`` (for :meth:`Pipeline.astream` and the agent loop's
    async stream). The put/put_nowait dispatch lives here so callers
    don't replicate the isinstance branching.

    Set ``include_heartbeats=False`` to drop ``is_heartbeat=True`` progress
    events at the emitter — useful for high-volume deployments where the
    keepalive overhead outweighs the SSE-proxy benefit.

    After :meth:`close`, subsequent :meth:`put` calls are silent no-ops.
    This lets abandoned background workers (after early stream break)
    drop events without crashing when the consumer has moved on.
    """

    def __init__(
        self,
        q: queue.Queue | asyncio.Queue,
        run_id: str | None = None,
        *,
        include_heartbeats: bool = True,
    ) -> None:
        self._q = q
        self.run_id = run_id
        self._closed = False
        self._include_heartbeats = include_heartbeats

    def put(self, event: StreamEvent | object) -> None:
        if self._closed:
            return
        # Filter heartbeat ticks at the emitter so they never reach the queue,
        # keeping high-volume deployments from buffering keepalive noise.
        if (
            not self._include_heartbeats
            and isinstance(event, StepProgressEvent)
            and event.is_heartbeat
        ):
            return
        if isinstance(self._q, asyncio.Queue):
            self._q.put_nowait(event)
        else:
            self._q.put(event)

    def close(self) -> None:
        """Emit the sentinel once, then silence further puts. Idempotent."""
        if self._closed:
            return
        # Sentinel must land on the queue before we flip the flag,
        # otherwise the drain loop would block forever.
        if isinstance(self._q, asyncio.Queue):
            self._q.put_nowait(_SENTINEL)
        else:
            self._q.put(_SENTINEL)
        self._closed = True

    def on_progress(self, ev: ProgressEvent) -> None:
        self.put(progress_to_stream_event(ev, self.run_id))

    def on_queued(self, ev: StepQueuedEvent) -> None:
        # Already a fully-formed StreamEvent — forward verbatim.
        self.put(ev)

    def on_retry(self, ev: StepRetriedEvent) -> None:
        # StepRetriedEvent is already a StreamEvent — forward as-is. The
        # provider layer fills run_id from RunnableConfig, so we don't
        # need to backfill from self.run_id here.
        self.put(ev)

    def on_step_complete(self, ev: StepCompleteEvent, run_id: str | None = None) -> None:
        # ``run_id`` lets a caller that already knows the run id for this
        # specific event pass it explicitly, since ``self.run_id`` is only
        # ever set at construction time (stream()/astream() build the
        # emitter before the run id exists) — see #87.
        self.put(step_complete_to_stream_event(ev, run_id if run_id is not None else self.run_id))


# Backward-compat alias for existing internal imports
_QueueEmitter = QueueEmitter


class EmitterSlot:
    """A thread/task-local slot holding the "active" stream emitter.

    ``Pipeline`` and ``AgentLoop`` install their per-run emitter here instead
    of on a plain instance attribute. A plain attribute is shared by every
    concurrent ``stream()``/``astream()`` call on the SAME instance, so the
    second call's install silently clobbers the first and events
    cross-deliver between consumers (#79, #84). Backed by
    ``contextvars.ContextVar``, whose value is isolated per OS thread (a new
    ``threading.Thread`` starts with its own empty top-level ``Context``) and
    per ``asyncio.Task`` (``create_task`` snapshots the context at creation)
    — exactly the isolation ``stream()``/``astream()`` already rely on for
    their one-worker-per-call model, so no additional locking is needed.
    """

    def __init__(self, name: str) -> None:
        self._var: contextvars.ContextVar[QueueEmitter | None] = contextvars.ContextVar(
            name, default=None
        )

    def get(self) -> QueueEmitter | None:
        return self._var.get()

    def set(self, emitter: QueueEmitter | None) -> QueueEmitter | None:
        """Install ``emitter`` for the calling thread/task; return the prior value."""
        prior = self._var.get()
        self._var.set(emitter)
        return prior


def drain_queue_sync(q: queue.Queue):
    while True:
        item = q.get()
        if item is _SENTINEL:
            return
        yield item


async def drain_queue_async(q: asyncio.Queue):
    while True:
        item = await q.get()
        if item is _SENTINEL:
            return
        yield item
