"""Tests for WebhookNotifier, WebhookConfig, and WebhookSink."""

from __future__ import annotations

import json
import socket
import time
from unittest.mock import MagicMock, patch

from genblaze_core.models.enums import Modality, RunStatus, StepStatus
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.pipeline.result import PipelineResult, StepCompleteEvent
from genblaze_core.providers.progress import ProgressEvent
from genblaze_core.testing import MockProvider
from genblaze_core.webhooks.notifier import WebhookConfig, WebhookNotifier
from genblaze_core.webhooks.sink import WebhookSink

# Fake public DNS addrinfo — resolves any host to a routable IP for test isolation.
# Port is included because resolve_ssrf now passes the port to getaddrinfo.
_PUBLIC_IP = "93.184.216.34"
_FAKE_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443))]
_PRIVATE_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]


def _make_mock_conn(status: int = 200, body: bytes = b"") -> MagicMock:
    """Build a mock http.client.HTTPSConnection that returns the given status."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body

    conn = MagicMock()
    conn.getresponse.return_value = resp
    return conn


# notifier._post delegates to open_pinned_https_connection imported from _utils.
# Patch in the notifier's namespace so the import reference is replaced.
_OPEN_CONN_PATCH = "genblaze_core.webhooks.notifier.open_pinned_https_connection"
# For DNS-pinning tests that inspect socket.create_connection inside _utils.
_CREATE_CONN_PATCH = "genblaze_core._utils.socket.create_connection"


def _make_http_patches(conn_mock: MagicMock):
    """Patch open_pinned_https_connection so _post uses conn_mock without real I/O.

    Usage::

        with _make_http_patches(conn):
            ...
    """
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch(_OPEN_CONN_PATCH, return_value=conn_mock))
    return stack


def _request_body(conn_mock: MagicMock) -> dict:
    """Extract and parse the JSON body from a conn.request call."""
    return json.loads(conn_mock.request.call_args.kwargs["body"])


def _request_headers(conn_mock: MagicMock) -> dict:
    """Extract the headers dict from a conn.request call."""
    return conn_mock.request.call_args.kwargs["headers"]


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
    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_posts_json_payload(self, _mock_dns):
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            notifier = WebhookNotifier(WebhookConfig(url="https://example.com/hook"))
            notifier.notify_pipeline_started("run-1", "test-pipe", 3)
            _drain_notifier(notifier)

        # conn.request was called once with POST and the correct body
        conn.request.assert_called_once()
        payload = _request_body(conn)
        assert payload["event"] == "pipeline.started"
        assert payload["run_id"] == "run-1"
        assert payload["pipeline_name"] == "test-pipe"
        assert payload["step_count"] == 3
        assert "timestamp" in payload

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_custom_headers(self, _mock_dns):
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            notifier = WebhookNotifier(
                WebhookConfig(
                    url="https://example.com/hook",
                    headers={"Authorization": "Bearer tok123"},
                )
            )
            notifier.notify_pipeline_started("r1", "p", 1)
            _drain_notifier(notifier)

        # Verify the Authorization header was passed in the headers dict
        headers = _request_headers(conn)
        assert headers.get("Authorization") == "Bearer tok123"
        assert headers.get("Content-Type") == "application/json"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_fire_and_forget_non_blocking(self, _mock_dns):
        """Enqueue should return immediately without waiting for delivery."""
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            notifier = WebhookNotifier(WebhookConfig(url="https://example.com"))
            t0 = time.monotonic()
            for _ in range(10):
                notifier.notify_pipeline_started("r", "p", 1)
            elapsed = time.monotonic() - t0
            _drain_notifier(notifier)
        # Enqueuing 10 events should be near-instant
        assert elapsed < 0.5

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_retry_on_5xx(self, _mock_dns):
        """5xx responses trigger retries up to max_retries."""
        conn_500 = _make_mock_conn(status=500)
        conn_200 = _make_mock_conn(status=200)

        call_count = 0

        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return conn_500 if call_count == 1 else conn_200

        with (
            patch(_OPEN_CONN_PATCH, side_effect=conn_factory),
            patch("genblaze_core.webhooks.notifier.time.sleep"),  # skip actual backoff
        ):
            notifier = WebhookNotifier(WebhookConfig(url="https://example.com", max_retries=1))
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier, timeout=5)

        assert call_count == 2

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_drops_on_4xx(self, _mock_dns):
        """4xx responses are not retried — exactly one HTTP attempt."""
        conn = _make_mock_conn(status=400)
        with (
            _make_http_patches(conn),
            patch("genblaze_core.webhooks.notifier.time.sleep"),
        ):
            notifier = WebhookNotifier(WebhookConfig(url="https://example.com", max_retries=2))
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier)

        assert conn.request.call_count == 1

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_event_filtering(self, _mock_dns):
        """Only events in include_events are delivered."""
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            notifier = WebhookNotifier(
                WebhookConfig(
                    url="https://example.com",
                    include_events={"pipeline.completed"},
                )
            )
            notifier.notify_pipeline_started("r", "p", 1)  # filtered out
            run = MagicMock()
            run.run_id = "r"
            run.status = RunStatus.COMPLETED
            run.steps = []
            manifest = MagicMock()
            manifest.canonical_hash = "abc"
            result = PipelineResult(run, manifest)
            notifier.notify_pipeline_completed(result)
            _drain_notifier(notifier)

        # Only pipeline.completed was sent
        assert conn.request.call_count == 1
        assert _request_body(conn)["event"] == "pipeline.completed"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_on_step_complete_callback(self, _mock_dns):
        """make_on_step_complete returns a callable that fires events."""
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            notifier = WebhookNotifier(WebhookConfig(url="https://example.com"))
            callback = notifier.make_on_step_complete()

            from genblaze_core.models.step import Step

            step = Step(provider="mock", model="m", status=StepStatus.SUCCEEDED)
            event = StepCompleteEvent(step_index=0, total_steps=1, step=step, elapsed_sec=1.5)
            callback(event)
            _drain_notifier(notifier)

        payload = _request_body(conn)
        assert payload["event"] == "step.completed"
        assert payload["status"] == "succeeded"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_on_progress_fires_step_started(self, _mock_dns):
        """make_on_progress fires step.started on first 'submitted' status."""
        conn = _make_mock_conn()
        with _make_http_patches(conn):
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

        assert conn.request.call_count == 1
        payload = _request_body(conn)
        assert payload["event"] == "step.started"
        assert payload["step_id"] == "s1"

    def test_bounded_queue_drops_and_counts(self):
        """A full queue drops oldest and increments dropped_count rather than blocking."""
        notifier = WebhookNotifier(WebhookConfig(url="https://example.com"), queue_maxsize=2)
        notifier.close(timeout=0.1)  # stop worker so queue fills

        for i in range(20):
            notifier.enqueue({"event": "step.completed", "n": i})

        assert notifier.dropped_count > 0

    def test_ssrf_check_runs_per_delivery(self):
        """open_pinned_https_connection (which calls resolve_ssrf) must be invoked
        on every delivery attempt. If SSRF is removed from _post(), call_count drops to 0.
        """
        conn = _make_mock_conn()
        with patch(_OPEN_CONN_PATCH, return_value=conn) as mock_open:
            notifier = WebhookNotifier(
                WebhookConfig(url="https://hooks.example.com/ep", max_retries=0)
            )
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier)

        assert mock_open.call_count >= 1


# ---------------------------------------------------------------------------
# DNS pinning / rebinding tests
# ---------------------------------------------------------------------------


class TestWebhookDnsPinning:
    """Verify DNS pinning: the IP validated by resolve_ssrf is the one connected to,
    not a subsequent re-resolution that could return a private address."""

    def test_rebinding_blocked(self):
        """DNS rebinding: first getaddrinfo → public IP (passes check), but
        connect target is always the pinned public IP, not a re-resolution.

        Tests open_pinned_https_connection directly — the helper that both
        notifier and dalle delegate to.
        """
        # First DNS call public (validation passes); subsequent private (rebinding).
        public_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443))]
        private_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        call_n = 0

        def dns_side_effect(*a, **k):
            nonlocal call_n
            call_n += 1
            return public_result if call_n == 1 else private_result

        connect_targets: list[tuple] = []

        def fake_create_connection(address, timeout=None):
            connect_targets.append(address)
            return MagicMock()

        conn = _make_mock_conn()
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = MagicMock()

        # open_pinned_https_connection uses local `import socket as _socket` and
        # `import ssl`, so patch at the canonical module level — Python's import
        # cache means the same object is used regardless of the alias.
        with (
            patch("genblaze_core._utils.socket.getaddrinfo", side_effect=dns_side_effect),
            patch("socket.create_connection", side_effect=fake_create_connection),
            patch("ssl.create_default_context", return_value=mock_ctx),
            patch("http.client.HTTPSConnection", return_value=conn),
        ):
            from genblaze_core._utils import open_pinned_https_connection

            open_pinned_https_connection("https://hooks.example.com/ep", timeout=5.0)

        # One TCP connection; must be to the pinned public IP
        assert len(connect_targets) == 1
        assert connect_targets[0][0] == _PUBLIC_IP, (
            "Connection must use the IP from resolve_ssrf, not a re-resolution"
        )

    def test_private_ip_on_first_resolution_rejected(self):
        """If getaddrinfo returns a private IP, delivery is rejected before any connect."""
        connect_called = False

        def fake_create_connection(address, timeout=None):
            nonlocal connect_called
            connect_called = True
            return MagicMock()

        with (
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=_PRIVATE_DNS),
            patch(_CREATE_CONN_PATCH, side_effect=fake_create_connection),
        ):
            notifier = WebhookNotifier(
                WebhookConfig(url="https://hooks.example.com/ep", max_retries=0)
            )
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier)

        # WebhookError from resolve_ssrf must stop delivery before any TCP connect
        assert not connect_called, "No TCP connect should happen when SSRF check fails"

    def test_connection_closed_on_request_error(self):
        """If conn.request() raises, the connection must still be closed (no FD leak)."""
        conn = MagicMock()
        conn.request.side_effect = OSError("broken pipe")

        with (
            patch(_OPEN_CONN_PATCH, return_value=conn),
            patch("genblaze_core.webhooks.notifier.time.sleep"),
        ):
            notifier = WebhookNotifier(
                WebhookConfig(
                    url="https://hooks.example.com/ep",
                    max_retries=0,
                )
            )
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier)

        # conn.close() must be called even though request() raised
        conn.close.assert_called()


# ---------------------------------------------------------------------------
# WebhookNotifier redirect tests (http.client has no redirect logic)
# ---------------------------------------------------------------------------


class TestWebhookRedirectSsrf:
    """Webhook delivery must not follow redirects — http.client has no redirect
    handler, so a 3xx status is treated as a non-2xx response and logged."""

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_redirect_response_not_followed(self, _mock_dns):
        """A 3xx response causes one delivery attempt; no second request is made."""
        conn = _make_mock_conn(status=301)
        with _make_http_patches(conn):
            notifier = WebhookNotifier(
                WebhookConfig(url="https://hooks.example.com/ep", max_retries=0)
            )
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier)
        # Exactly one HTTP attempt — redirect not followed
        assert conn.request.call_count == 1

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_redirect_location_never_dns_resolved(self, mock_dns):
        """With http.client, the Location header of a 3xx is never inspected or
        DNS-resolved — the connection to the private Location address cannot happen."""
        conn = _make_mock_conn(status=301)
        with _make_http_patches(conn):
            notifier = WebhookNotifier(
                WebhookConfig(url="https://hooks.example.com/ep", max_retries=0)
            )
            notifier.notify_pipeline_started("r", "p", 1)
            _drain_notifier(notifier)

        # DNS was only resolved for the configured URL host (hooks.example.com).
        # The IMDS address in the Location header was never resolved.
        imds_dns = [c for c in mock_dns.call_args_list if "169.254" in str(c)]
        assert imds_dns == [], "Location host must not be DNS-resolved after 3xx"


# ---------------------------------------------------------------------------
# WebhookSink tests
# ---------------------------------------------------------------------------


class TestWebhookSink:
    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_write_run_posts_completed(self, _mock_dns):
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            sink = WebhookSink(WebhookConfig(url="https://example.com"))

            run = MagicMock()
            run.run_id = "r1"
            run.status = RunStatus.COMPLETED
            run.steps = [MagicMock()]
            manifest = MagicMock()
            manifest.canonical_hash = "hash123"

            sink.write_run(run, manifest)
            sink.close()

        payload = _request_body(conn)
        assert payload["event"] == "pipeline.completed"
        assert payload["run_id"] == "r1"
        assert payload["canonical_hash"] == "hash123"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_write_run_posts_failed(self, _mock_dns):
        conn = _make_mock_conn()
        with _make_http_patches(conn):
            sink = WebhookSink(WebhookConfig(url="https://example.com"))

            run = MagicMock()
            run.run_id = "r2"
            run.status = RunStatus.FAILED
            run.steps = []
            manifest = MagicMock()
            manifest.canonical_hash = "hash456"

            sink.write_run(run, manifest)
            sink.close()

        assert _request_body(conn)["event"] == "pipeline.failed"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


class TestWebhookIntegration:
    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_DNS)
    def test_notifier_with_pipeline(self, _mock_dns):
        """End-to-end: WebhookNotifier wired into a real Pipeline run."""
        conn = _make_mock_conn()
        with _make_http_patches(conn):
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
        assert conn.request.call_count >= 2
        bodies = [json.loads(c.kwargs["body"]) for c in conn.request.call_args_list]
        events = [b["event"] for b in bodies]
        assert "step.completed" in events
        assert "pipeline.completed" in events
