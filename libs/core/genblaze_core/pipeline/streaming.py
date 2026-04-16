"""Streaming helpers — bridge pipeline callbacks into push-style event queues.

Used by :meth:`Pipeline.stream`, :meth:`Pipeline.astream`, and the agent
loop streams to emit a single ordered stream of :class:`StreamEvent`
instances. The emitter wraps the existing ``on_progress`` and
``on_step_complete`` callbacks so no provider changes are required.
"""

from __future__ import annotations

import asyncio
import queue
from typing import TYPE_CHECKING

from genblaze_core.observability.events import StreamEvent

if TYPE_CHECKING:
    from genblaze_core.pipeline.result import StepCompleteEvent
    from genblaze_core.providers.progress import ProgressEvent


_SENTINEL = object()


def progress_to_stream_event(ev: ProgressEvent, run_id: str | None = None) -> StreamEvent:
    """Map a provider ProgressEvent to a StreamEvent(type=step.progress).

    Single source of truth for this translation — used by both the
    queue-backed emitter and Pipeline's in-process progress fan-out.
    """
    return StreamEvent(
        type="step.progress",
        run_id=run_id,
        step_id=ev.step_id,
        provider=ev.provider,
        model=ev.model,
        progress_pct=ev.progress_pct,
        elapsed_sec=ev.elapsed_sec,
        preview_url=ev.preview_url,
        message=ev.message,
        data={"status": ev.status},
    )


def step_complete_to_stream_event(ev: StepCompleteEvent, run_id: str | None = None) -> StreamEvent:
    """Map a pipeline StepCompleteEvent to a StreamEvent."""
    from genblaze_core.models.enums import StepStatus

    failed = ev.step.status == StepStatus.FAILED
    return StreamEvent(
        type="step.failed" if failed else "step.completed",
        run_id=run_id,
        step_id=ev.step.step_id,
        step_index=ev.step_index,
        total_steps=ev.total_steps,
        provider=ev.step.provider,
        model=ev.step.model,
        elapsed_sec=ev.elapsed_sec,
        step=ev.step,
        message=ev.step.error if failed else None,
    )


class QueueEmitter:
    """Pushes StreamEvents onto a queue — works with both queue types.

    Accepts either a ``queue.Queue`` (for :meth:`Pipeline.stream`) or an
    ``asyncio.Queue`` (for :meth:`Pipeline.astream` and the agent loop's
    async stream). The put/put_nowait dispatch lives here so callers
    don't replicate the isinstance branching.
    """

    def __init__(self, q: queue.Queue | asyncio.Queue, run_id: str | None = None) -> None:
        self._q = q
        self.run_id = run_id

    def put(self, event: StreamEvent | object) -> None:
        if isinstance(self._q, asyncio.Queue):
            self._q.put_nowait(event)
        else:
            self._q.put(event)

    def close(self) -> None:
        self.put(_SENTINEL)

    def on_progress(self, ev: ProgressEvent) -> None:
        self.put(progress_to_stream_event(ev, self.run_id))

    def on_step_complete(self, ev: StepCompleteEvent) -> None:
        self.put(step_complete_to_stream_event(ev, self.run_id))


# Backward-compat alias for existing internal imports
_QueueEmitter = QueueEmitter


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
