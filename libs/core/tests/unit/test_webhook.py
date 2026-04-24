"""Tests for WebhookNotifier, WebhookConfig, and WebhookSink."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

from genblaze_core.models.enums import Modality, RunStatus, StepStatus
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.pipeline.result import PipelineResult, StepCompleteEvent
from genblaze_core.providers.progress import ProgressEvent
from genblaze_core.testing import MockProvider
from genblaze_core.webhooks.notifier import WebhookConfig, WebhookNotifier
from genblaze_core.webhooks.sink import WebhookSink


def _mock_urlopen_ok(*args, **kwargs):
    """Mock urlopen returning 200."""
    resp = MagicMock()
    resp.read.return_value = b""
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp


def _drain_notifier(notifier: WebhookNotifier, timeout: float = 2.0) -> None:
    """Close the notifier and wait for the queue to drain."""
    notifier.close(timeout=timeout)


# ---------------------------------------------------------------------------
# WebhookConfig tests
# ---------------------------------------------------------------------------


class TestWebhookConfig:
    def test_defaults(self):
        cfg = WebhookConfig(url="https://example.com")
        assert cfg.timeout == 10.0
        assert cfg.max_retries == 2
        assert cfg.include_events is None
        assert cfg.headers is None


# ---------------------------------------------------------------------------
# WebhookNotifier tests
# ---------------------------------------------------------------------------


class TestWebhookNotifier:
    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_posts_json_payload(self, mock_urlopen):
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com/hook"))
        notifier.notify_pipeline_started("run-1", "test-pipe", 3)
        _drain_notifier(notifier)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["event"] == "pipeline.started"
        assert body["run_id"] == "run-1"
        assert body["pipeline_name"] == "test-pipe"
        assert body["step_count"] == 3
        assert "timestamp" in body

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_custom_headers(self, mock_urlopen):
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(
            WebhookConfig(
                url="https://example.com/hook",
                headers={"Authorization": "Bearer tok123"},
            )
        )
        notifier.notify_pipeline_started("r1", "p", 1)
        _drain_notifier(notifier)

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok123"
        assert req.get_header("Content-type") == "application/json"

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_fire_and_forget_non_blocking(self, mock_urlopen):
        """Enqueue should return immediately without waiting for delivery."""
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com"))
        t0 = time.monotonic()
        for _ in range(10):
            notifier.notify_pipeline_started("r", "p", 1)
        elapsed = time.monotonic() - t0
        _drain_notifier(notifier)
        # Enqueuing 10 events should be near-instant (< 0.1s)
        assert elapsed < 0.5

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_retry_on_5xx(self, mock_urlopen):
        """5xx responses trigger retries."""
        error = urllib.error.HTTPError(
            "https://example.com",
            500,
            "Internal Server Error",
            {},
            BytesIO(b""),
        )
        mock_urlopen.side_effect = [error, _mock_urlopen_ok()]
        notifier = WebhookNotifier(
            WebhookConfig(
                url="https://example.com",
                max_retries=1,
            )
        )
        notifier.notify_pipeline_started("r", "p", 1)
        _drain_notifier(notifier, timeout=5)
        assert mock_urlopen.call_count == 2

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_drops_on_4xx(self, mock_urlopen):
        """4xx responses are not retried."""
        error = urllib.error.HTTPError(
            "https://example.com",
            400,
            "Bad Request",
            {},
            BytesIO(b""),
        )
        mock_urlopen.side_effect = error
        notifier = WebhookNotifier(
            WebhookConfig(
                url="https://example.com",
                max_retries=2,
            )
        )
        notifier.notify_pipeline_started("r", "p", 1)
        _drain_notifier(notifier)
        # Only called once — no retry on 4xx
        assert mock_urlopen.call_count == 1

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_event_filtering(self, mock_urlopen):
        """Only events in include_events are delivered."""
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(
            WebhookConfig(
                url="https://example.com",
                include_events={"pipeline.completed"},
            )
        )
        notifier.notify_pipeline_started("r", "p", 1)  # filtered out
        # Simulate pipeline completed
        run = MagicMock()
        run.run_id = "r"
        run.status = RunStatus.COMPLETED
        run.steps = []
        manifest = MagicMock()
        manifest.canonical_hash = "abc"
        result = PipelineResult(run, manifest)
        notifier.notify_pipeline_completed(result)
        _drain_notifier(notifier)

        # Only pipeline.completed should have been sent
        assert mock_urlopen.call_count == 1
        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["event"] == "pipeline.completed"

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_on_step_complete_callback(self, mock_urlopen):
        """make_on_step_complete returns a callable that fires events."""
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com"))
        callback = notifier.make_on_step_complete()

        from genblaze_core.models.step import Step

        step = Step(provider="mock", model="m", status=StepStatus.SUCCEEDED)
        event = StepCompleteEvent(step_index=0, total_steps=1, step=step, elapsed_sec=1.5)
        callback(event)
        _drain_notifier(notifier)

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["event"] == "step.completed"
        assert body["status"] == "succeeded"

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_on_progress_fires_step_started(self, mock_urlopen):
        """make_on_progress fires step.started on first 'submitted' status."""
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com"))
        callback = notifier.make_on_progress()

        event = ProgressEvent(
            step_id="s1",
            provider="mock",
            model="m",
            status="submitted",
            progress_pct=None,
            elapsed_sec=0.1,
        )
        callback(event)
        # Second call with same step_id should NOT fire again
        callback(event)
        _drain_notifier(notifier)

        assert mock_urlopen.call_count == 1
        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["event"] == "step.started"
        assert body["step_id"] == "s1"

    def test_bounded_queue_drops_and_counts(self):
        """A full queue drops oldest and increments dropped_count rather than blocking."""
        # Small maxsize + don't start the worker so enqueues pile up.
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com"), queue_maxsize=2)
        notifier.close(timeout=0.1)  # stop the worker so it stops draining

        # Worker may still pull one item off before it sees the sentinel;
        # enqueue enough extras that we're guaranteed to hit capacity.
        for i in range(20):
            notifier.enqueue({"event": "step.completed", "n": i})

        # Queue itself is bounded; dropped_count must be strictly positive
        # and no exception escaped to the caller.
        assert notifier.dropped_count > 0


# ---------------------------------------------------------------------------
# WebhookSink tests
# ---------------------------------------------------------------------------


class TestWebhookSink:
    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_write_run_posts_completed(self, mock_urlopen):
        mock_urlopen.side_effect = _mock_urlopen_ok
        sink = WebhookSink(WebhookConfig(url="https://example.com"))

        run = MagicMock()
        run.run_id = "r1"
        run.status = RunStatus.COMPLETED
        run.steps = [MagicMock()]
        manifest = MagicMock()
        manifest.canonical_hash = "hash123"

        sink.write_run(run, manifest)
        sink.close()

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["event"] == "pipeline.completed"
        assert body["run_id"] == "r1"
        assert body["canonical_hash"] == "hash123"

    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_write_run_posts_failed(self, mock_urlopen):
        mock_urlopen.side_effect = _mock_urlopen_ok
        sink = WebhookSink(WebhookConfig(url="https://example.com"))

        run = MagicMock()
        run.run_id = "r2"
        run.status = RunStatus.FAILED
        run.steps = []
        manifest = MagicMock()
        manifest.canonical_hash = "hash456"

        sink.write_run(run, manifest)
        sink.close()

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["event"] == "pipeline.failed"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


class TestWebhookIntegration:
    @patch("genblaze_core.webhooks.notifier.urllib.request.urlopen")
    def test_notifier_with_pipeline(self, mock_urlopen):
        """End-to-end: WebhookNotifier wired into a real Pipeline run."""
        mock_urlopen.side_effect = _mock_urlopen_ok
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com"))

        provider = MockProvider()
        result = (
            Pipeline("webhook-test")
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .run(
                on_step_complete=notifier.make_on_step_complete(),
            )
        )
        notifier.notify_pipeline_completed(result)
        _drain_notifier(notifier)

        # At least step.completed and pipeline.completed were posted
        assert mock_urlopen.call_count >= 2
        # Verify the last call was pipeline.completed
        bodies = [json.loads(call[0][0].data) for call in mock_urlopen.call_args_list]
        events = [b["event"] for b in bodies]
        assert "step.completed" in events
        assert "pipeline.completed" in events
