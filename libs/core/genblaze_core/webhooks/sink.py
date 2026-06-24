"""WebhookSink — BaseSink that posts pipeline completion events via webhook."""

from __future__ import annotations

from genblaze_core._utils import utc_now
from genblaze_core.models.enums import RunStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.sinks.base import BaseSink
from genblaze_core.webhooks.notifier import WebhookConfig, WebhookEvent, WebhookNotifier


class WebhookSink(BaseSink):
    """Sink that posts pipeline.completed/failed events to a webhook URL.

    Convenience for users who only want completion notifications without
    wiring up progress/step callbacks.

    Example::

        sink = WebhookSink(WebhookConfig(url="https://hooks.example.com/gen"))
        Pipeline("my-pipe").step(...).run(sink=sink)
    """

    # Fire-and-forget: the notifier's daemon worker flushes via atexit, so the
    # pipeline must NOT close (and block joining) this sink at end of run. That
    # keeps delivery non-blocking and the sink reusable across runs. Callers who
    # want a synchronous flush still call close()/`with sink:` explicitly.
    _close_with_run = False

    def __init__(self, config: WebhookConfig) -> None:
        self._notifier = WebhookNotifier(config)

    def write_run(self, run: Run, manifest: Manifest) -> None:
        """Post a pipeline.completed or pipeline.failed event."""
        status = run.status
        event = (
            WebhookEvent.PIPELINE_COMPLETED
            if status == RunStatus.COMPLETED
            else WebhookEvent.PIPELINE_FAILED
        )
        self._notifier.enqueue(
            {
                "event": event,
                "run_id": run.run_id,
                "status": str(status),
                "step_count": len(run.steps),
                "canonical_hash": manifest.canonical_hash,
                "timestamp": utc_now().isoformat(),
            }
        )

    def close(self) -> None:
        """Flush and stop the webhook worker thread."""
        self._notifier.close()
