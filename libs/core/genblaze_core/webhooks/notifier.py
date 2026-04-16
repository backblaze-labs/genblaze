"""WebhookNotifier — fire-and-forget HTTP notifications for pipeline events.

Uses a background daemon thread with a queue to avoid blocking the pipeline.
Only stdlib dependencies (urllib.request, threading, queue).
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
import weakref
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from genblaze_core._utils import jittered_backoff, utc_now
from genblaze_core.exceptions import WebhookError
from genblaze_core.models.enums import RunStatus, StepStatus
from genblaze_core.pipeline.result import PipelineResult, StepCompleteEvent
from genblaze_core.providers.progress import ProgressEvent

logger = logging.getLogger("genblaze.webhook")

# Sentinel to signal the worker thread to stop
_STOP = object()


class WebhookEvent(StrEnum):
    """Webhook event types."""

    PIPELINE_STARTED = "pipeline.started"
    PIPELINE_COMPLETED = "pipeline.completed"
    PIPELINE_FAILED = "pipeline.failed"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"


@dataclass
class WebhookConfig:
    """Configuration for webhook notifications.

    Attributes:
        url: Target URL to POST events to.
        headers: Extra HTTP headers (e.g. {"Authorization": "Bearer ..."}).
        timeout: HTTP request timeout in seconds.
        max_retries: Number of retries on 5xx errors.
        include_events: Optional set of event names to send.
            None means all events. E.g. {WebhookEvent.STEP_COMPLETED}.
    """

    url: str
    headers: dict[str, str] | None = None
    timeout: float = 10.0
    max_retries: int = 2
    include_events: set[str] | None = None

    def __post_init__(self) -> None:
        """Validate webhook URL format (no DNS — that happens at dispatch time)."""
        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        if parsed.scheme not in ("https",):
            raise WebhookError(f"Webhook URL must use HTTPS, got: {parsed.scheme}://")

        host = parsed.hostname or ""
        if not host:
            raise WebhookError(f"Webhook URL is missing a hostname: {self.url}")
        if host.lower() == "localhost":
            raise WebhookError(f"Webhook URL cannot target private/loopback hosts: {host}")


class WebhookNotifier:
    """Fire-and-forget webhook dispatcher for pipeline events.

    Delivers JSON payloads to a configured URL via a background thread.
    Events are queued and delivered asynchronously — pipeline execution
    is never blocked by webhook delivery.

    Example::

        notifier = WebhookNotifier(WebhookConfig(url="https://hooks.example.com/gen"))
        result = Pipeline("my-pipe").step(...).run(
            on_progress=notifier.make_on_progress(),
            on_step_complete=notifier.make_on_step_complete(),
        )
        notifier.notify_pipeline_completed(result)
        notifier.close()
    """

    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()
        # Track which steps have fired "step.started" (avoid duplicates)
        self._started_steps: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._ssrf_validated_at: float = 0.0  # monotonic timestamp of last SSRF check
        self._ssrf_ttl: float = 60.0  # Re-validate DNS every 60s to limit rebinding window
        # Register cleanup via atexit with a weak reference to avoid preventing GC
        self._atexit_ref = weakref.ref(self)
        atexit.register(WebhookNotifier._atexit_close, self._atexit_ref)

    def _should_send(self, event: str) -> bool:
        """Check if this event type passes the include filter."""
        if self._config.include_events is None:
            return True
        return event in self._config.include_events

    def enqueue(self, payload: dict[str, Any]) -> None:
        """Add a payload to the delivery queue (filtered by include_events)."""
        event = payload.get("event", "")
        if self._should_send(event):
            self._queue.put(payload)

    def _validate_ssrf(self) -> None:
        """DNS-resolve the webhook host and block private IPs.

        Re-validates periodically (every ``_ssrf_ttl`` seconds) to prevent
        DNS rebinding attacks while avoiding a DNS lookup on every delivery.
        """
        now = time.monotonic()
        if now - self._ssrf_validated_at < self._ssrf_ttl:
            return
        from genblaze_core._utils import check_ssrf

        check_ssrf(self._config.url, exc_type=WebhookError)
        self._ssrf_validated_at = now

    def _post(self, payload: dict[str, Any]) -> None:
        """POST a JSON payload to the configured URL with retries."""
        self._validate_ssrf()
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.headers:
            headers.update(self._config.headers)

        for attempt in range(self._config.max_retries + 1):
            try:
                req = urllib.request.Request(  # noqa: S310
                    self._config.url,
                    data=data,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:  # noqa: S310
                    resp.read()  # Drain response body
                return
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and attempt < self._config.max_retries:
                    backoff = jittered_backoff(attempt)
                    logger.debug("Webhook 5xx (%d), retrying in %.1fs", exc.code, backoff)
                    time.sleep(backoff)
                    continue
                logger.warning("Webhook delivery failed: HTTP %d", exc.code)
                return
            except Exception as exc:
                if attempt < self._config.max_retries:
                    time.sleep(jittered_backoff(attempt))
                    continue
                logger.warning("Webhook delivery failed: %s", exc)
                return

    def _drain(self) -> None:
        """Worker thread: drain the queue and POST each payload."""
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                break
            try:
                self._post(item)
            except Exception:
                logger.exception("Unexpected error in webhook worker")
            finally:
                self._queue.task_done()

    # --- Public API: event notification methods ---

    def notify_pipeline_started(
        self,
        run_id: str,
        pipeline_name: str | None,
        step_count: int,
    ) -> None:
        """Fire a pipeline.started event."""
        self.enqueue(
            {
                "event": WebhookEvent.PIPELINE_STARTED,
                "run_id": run_id,
                "pipeline_name": pipeline_name,
                "step_count": step_count,
                "timestamp": utc_now().isoformat(),
            }
        )

    def notify_pipeline_completed(
        self,
        result: PipelineResult,
        *,
        canonical_hash: str | None = None,
    ) -> None:
        """Fire a pipeline.completed or pipeline.failed event."""
        status = result.run.status
        event = (
            WebhookEvent.PIPELINE_COMPLETED
            if status == RunStatus.COMPLETED
            else WebhookEvent.PIPELINE_FAILED
        )
        payload: dict[str, Any] = {
            "event": event,
            "run_id": result.run.run_id,
            "status": str(status),
            "step_count": len(result.run.steps),
            "timestamp": utc_now().isoformat(),
        }
        if canonical_hash is not None:
            payload["canonical_hash"] = canonical_hash
        self.enqueue(payload)

    def make_on_progress(self) -> Any:
        """Return a callback for Pipeline's on_progress parameter.

        Fires "step.started" on first "submitted" status per step.
        """

        def callback(event: ProgressEvent) -> None:
            if event.status != "submitted":
                return
            with self._lock:
                if event.step_id in self._started_steps:
                    return
                self._started_steps.add(event.step_id)
            self.enqueue(
                {
                    "event": WebhookEvent.STEP_STARTED,
                    "step_id": event.step_id,
                    "provider": event.provider,
                    "model": event.model,
                    "timestamp": utc_now().isoformat(),
                }
            )

        return callback

    def make_on_step_complete(self) -> Any:
        """Return a callback for Pipeline's on_step_complete parameter.

        Fires "step.completed" or "step.failed" per step.
        """

        def callback(event: StepCompleteEvent) -> None:
            step = event.step
            is_success = step.status == StepStatus.SUCCEEDED
            self.enqueue(
                {
                    "event": WebhookEvent.STEP_COMPLETED
                    if is_success
                    else WebhookEvent.STEP_FAILED,
                    "step_id": step.step_id,
                    "step_index": event.step_index,
                    "total_steps": event.total_steps,
                    "provider": step.provider,
                    "model": step.model,
                    "status": str(step.status),
                    "elapsed_sec": round(event.elapsed_sec, 2),
                    "timestamp": utc_now().isoformat(),
                }
            )

        return callback

    def close(self, timeout: float = 5.0) -> None:
        """Flush remaining events and stop the worker thread. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._worker.join(timeout=timeout)

    @staticmethod
    def _atexit_close(ref: weakref.ref) -> None:
        """atexit handler — safely flush remaining events at interpreter exit."""
        notifier = ref()
        if notifier is not None and notifier._worker.is_alive():
            notifier.close(timeout=3.0)
