"""Tests for the B2 403-preflight region probe (``_probe_other_b2_regions``).

Closes the "blames credentials" failure mode from the 2026-05-23 feedback
batch (item 6). On 403 from the user's configured B2 region — which some
B2 regions return instead of 301 for cross-region buckets — the preflight
probes the other B2 regions in parallel and surfaces a specific region
name in the error message.

These tests *cannot* use moto: moto only simulates AWS S3, not B2's
regional endpoints. Mocks land at the ``boto3.client`` factory boundary
so each probe call gets its own client with a per-region ``head_bucket``
verdict.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import StorageError

from tests.conftest import _FakeClientError

_B2_ENDPOINT = "https://s3.us-west-004.backblazeb2.com"


def _client_error(status: int, code: str = "Forbidden") -> _FakeClientError:
    """Build a fake botocore ``ClientError`` for a given HTTP status."""
    return _FakeClientError(
        response={
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {}},
        },
        operation_name="HeadBucket",
    )


def _dispatch_clients(mock_boto3, *, primary: MagicMock, per_region: dict[str, MagicMock]):
    """Route ``boto3.client("s3", endpoint_url=...)`` calls.

    The first call (from the backend ctor) gets the ``primary`` mock — the
    user's configured B2 region. Subsequent calls from
    ``_probe_other_b2_regions`` carry a region-specific ``endpoint_url``;
    we match on the embedded region slug.

    Returns a callable suitable for assignment to ``mock_boto3.client.side_effect``.
    """

    def _side_effect(*args, **kwargs):
        endpoint = kwargs.get("endpoint_url", "")
        for region, client in per_region.items():
            # Exact-match the full host so a future B2 region whose slug
            # is a substring of another (e.g. ``us-west-002`` vs a
            # hypothetical ``us-west-0024``) wouldn't misroute.
            if f"s3.{region}.backblazeb2.com" in endpoint:
                return client
        return primary

    return _side_effect


def _make_unverified_backend(mock_boto3, **overrides):
    """Construct a backend with preflight NOT yet run.

    Returns ``(backend, primary_client)``. The caller wires
    ``primary_client.head_bucket.side_effect`` and any per-region probe
    mocks via ``_dispatch_clients`` before invoking
    ``backend._ensure_region_verified()``.
    """
    from genblaze_s3.backend import S3StorageBackend

    primary = MagicMock()
    mock_boto3.client.return_value = primary
    defaults = {
        "bucket": "my-bucket",
        "endpoint_url": _B2_ENDPOINT,
        "region": "us-west-004",
        "aws_access_key_id": "AKIAEXAMPLE",
        "aws_secret_access_key": "secret",
    }
    defaults.update(overrides)
    backend = S3StorageBackend(**defaults)
    # NOTE: NOT setting _region_verified=True — the whole point is to
    # exercise the preflight error path.
    return backend, primary


# ---------------------------------------------------------------------------
# Outcome matrix — one 200, uniform 404, mixed
# ---------------------------------------------------------------------------


class TestRegionProbeOutcomes:
    def test_exactly_one_other_region_returns_200_names_it(self, mock_boto3):
        """The clean wrong-region case: user pointed at us-west-004,
        bucket lives in us-east-005. Error message must name the region."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        probe_clients = {
            "us-west-002": MagicMock(),
            "us-east-005": MagicMock(),
            "eu-central-003": MagicMock(),
        }
        probe_clients["us-west-002"].head_bucket.side_effect = _client_error(403)
        probe_clients["us-east-005"].head_bucket.return_value = {}  # 200 OK
        probe_clients["eu-central-003"].head_bucket.side_effect = _client_error(403)

        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=probe_clients
        )

        with pytest.raises(StorageError) as excinfo:
            backend._ensure_region_verified()

        msg = str(excinfo.value)
        # Substring match — wording can shift, info content can't.
        assert "lives in" in msg
        assert "us-east-005" in msg
        assert "for_backblaze()" in msg or "$B2_REGION" in msg

    def test_uniform_404_across_probed_regions_means_missing_bucket(self, mock_boto3):
        """Every probed region returns 404. Even though the user's region
        returned 403 (B2 hiding existence), uniform 404 elsewhere is
        authoritative: bucket truly doesn't exist."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        probe_clients = {r: MagicMock() for r in ("us-west-002", "us-east-005", "eu-central-003")}
        for c in probe_clients.values():
            c.head_bucket.side_effect = _client_error(404, code="NoSuchBucket")

        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=probe_clients
        )

        with pytest.raises(StorageError) as excinfo:
            backend._ensure_region_verified()

        msg = str(excinfo.value)
        assert "does not exist" in msg
        assert "B2 region" in msg

    def test_single_404_with_other_403s_falls_through_to_generic_message(self, mock_boto3):
        """One 404 + other 403s is NOT uniform 404. Must fall through to
        the generic "check name/region/credentials" message, not the
        missing-bucket message. Guards against a one-region blip
        misclassifying."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        probe_clients = {r: MagicMock() for r in ("us-west-002", "us-east-005", "eu-central-003")}
        probe_clients["us-west-002"].head_bucket.side_effect = _client_error(404)
        probe_clients["us-east-005"].head_bucket.side_effect = _client_error(403)
        probe_clients["eu-central-003"].head_bucket.side_effect = _client_error(403)

        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=probe_clients
        )

        with pytest.raises(StorageError) as excinfo:
            backend._ensure_region_verified()

        msg = str(excinfo.value)
        # NOT the missing-bucket branch
        assert "does not exist" not in msg
        # IS the generic fall-through, with the endpoint URL we tried
        assert "Check bucket name, region, and credentials" in msg
        assert _B2_ENDPOINT in msg

    def test_all_regions_403_falls_through_to_generic_message(self, mock_boto3):
        """All 403s. Could be bad creds, hidden buckets, or both. No
        region-specific signal — fall through to the generic message."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        probe_clients = {r: MagicMock() for r in ("us-west-002", "us-east-005", "eu-central-003")}
        for c in probe_clients.values():
            c.head_bucket.side_effect = _client_error(403)

        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=probe_clients
        )

        with pytest.raises(StorageError) as excinfo:
            backend._ensure_region_verified()

        msg = str(excinfo.value)
        assert "Check bucket name, region, and credentials" in msg
        assert _B2_ENDPOINT in msg


# ---------------------------------------------------------------------------
# B2-only gating — must not fire for non-B2 backends or non-403 errors
# ---------------------------------------------------------------------------


class TestRegionProbeGating:
    def test_does_not_fire_for_aws_s3_endpoint(self, mock_boto3):
        """Generic AWS S3 backend (no ``backblazeb2.com`` in endpoint) must
        NOT trigger the B2 region probe — the regions list is B2-specific."""
        backend, primary = _make_unverified_backend(
            mock_boto3,
            endpoint_url="https://s3.us-west-2.amazonaws.com",
            region="us-west-2",
        )
        primary.head_bucket.side_effect = _client_error(403)

        # Track probe calls: any extra boto3.client() invocation beyond
        # the constructor's one means the probe fired.
        calls_before_preflight = mock_boto3.client.call_count

        with pytest.raises(StorageError) as excinfo:
            backend._ensure_region_verified()

        # No extra clients constructed during preflight → no probe ran.
        assert mock_boto3.client.call_count == calls_before_preflight
        # Generic message, not the region-named one.
        assert "lives in" not in str(excinfo.value)

    def test_does_not_fire_for_r2_endpoint(self, mock_boto3):
        """Cloudflare R2 isn't B2 — same gating."""
        backend, primary = _make_unverified_backend(
            mock_boto3,
            endpoint_url="https://abc123.r2.cloudflarestorage.com",
            region="auto",
        )
        primary.head_bucket.side_effect = _client_error(403)

        calls_before = mock_boto3.client.call_count

        with pytest.raises(StorageError):
            backend._ensure_region_verified()

        assert mock_boto3.client.call_count == calls_before

    def test_does_not_fire_on_5xx_errors(self, mock_boto3):
        """5xx is transient. Probe is for 403 only — the existing transient-
        retry path stays intact."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(503, code="ServiceUnavailable")

        calls_before = mock_boto3.client.call_count

        with pytest.raises(StorageError):
            backend._ensure_region_verified()

        assert mock_boto3.client.call_count == calls_before

    def test_does_not_fire_on_301_redirect(self, mock_boto3):
        """301 with ``x-amz-bucket-region`` is the existing auto-correct
        path. Probe must not duplicate work or interfere."""
        backend, primary = _make_unverified_backend(mock_boto3)
        # 301 returns the actual region in headers — the existing path
        # consumes this and reconfigures without raising.
        primary.head_bucket.side_effect = _FakeClientError(
            response={
                "Error": {"Code": "PermanentRedirect"},
                "ResponseMetadata": {
                    "HTTPStatusCode": 301,
                    "HTTPHeaders": {"x-amz-bucket-region": "us-east-005"},
                },
            },
            operation_name="HeadBucket",
        )

        # Replace boto3.client with a fresh per-region client for the
        # reconfigure call (else it returns the original mock with the
        # still-301 side_effect, creating an infinite loop).
        reconfigured = MagicMock()
        reconfigured.head_bucket.return_value = {}
        mock_boto3.client.side_effect = [primary, reconfigured]

        # Should NOT raise — existing 301 auto-correct succeeds.
        backend._ensure_region_verified()
        assert backend._region == "us-east-005"


# ---------------------------------------------------------------------------
# Credential sharing + timeout config
# ---------------------------------------------------------------------------


class TestRegionProbeClientConfig:
    def test_probe_clients_share_credentials(self, mock_boto3):
        """Every probe client is constructed with the same access key /
        secret as the primary client. Otherwise the probe would hit
        anonymous-mode 403s on every region and misclassify."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        # Use a regular MagicMock per region; we only need to count calls
        # and inspect kwargs.
        per_region = {r: MagicMock() for r in ("us-west-002", "us-east-005", "eu-central-003")}
        for c in per_region.values():
            c.head_bucket.side_effect = _client_error(403)
        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=per_region
        )

        with pytest.raises(StorageError):
            backend._ensure_region_verified()

        # Inspect every call AFTER the first (primary client construction).
        probe_calls = mock_boto3.client.call_args_list[1:]
        assert len(probe_calls) == 3
        for call in probe_calls:
            assert call.kwargs.get("aws_access_key_id") == "AKIAEXAMPLE"
            assert call.kwargs.get("aws_secret_access_key") == "secret"

    def test_probe_clients_carry_short_timeout_config(self, mock_boto3):
        """A hung region can't extend the error path past ~one timeout.
        Probe clients get a short connect+read timeout and disabled retries.

        Intercepts the ``botocore.config.Config(...)`` constructor calls
        and asserts the probe builds Config with the documented aggressive
        values — not the default 30s connect / 300s read / 3 retries that
        ``_build_boto_config`` produces for the primary client.
        """
        import sys

        from genblaze_s3.backend import _PROBE_TIMEOUT_SEC

        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        per_region = {r: MagicMock() for r in ("us-west-002", "us-east-005", "eu-central-003")}
        for c in per_region.values():
            c.head_bucket.side_effect = _client_error(403)
        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=per_region
        )

        # Reset the ``Config`` constructor's call history so we only see
        # the calls made during this preflight, not the primary client
        # construction from the backend ctor.
        config_factory = sys.modules["botocore.config"].Config
        config_factory.reset_mock()

        with pytest.raises(StorageError):
            backend._ensure_region_verified()

        # Filter to the probe-built Configs (distinguished by the short
        # connect_timeout). The default _build_boto_config uses 30 → no
        # match. One Config per probed region: three regions, three calls.
        probe_config_calls = [
            call
            for call in config_factory.call_args_list
            if call.kwargs.get("connect_timeout") == _PROBE_TIMEOUT_SEC
        ]
        assert len(probe_config_calls) == 3, (
            f"expected 3 probe-config builds with connect_timeout={_PROBE_TIMEOUT_SEC}, "
            f"saw {len(probe_config_calls)} in {config_factory.call_args_list}"
        )
        for call in probe_config_calls:
            assert call.kwargs["connect_timeout"] == _PROBE_TIMEOUT_SEC
            assert call.kwargs["read_timeout"] == _PROBE_TIMEOUT_SEC
            assert call.kwargs["retries"] == {"max_attempts": 1}
            # b2ai-genblaze attribution must survive — probe traffic shows
            # up in B2 audit logs alongside primary traffic and must be
            # identifiable as genblaze, not as anonymous boto3.
            assert "b2ai-genblaze" in call.kwargs.get("user_agent_extra", "")

    def test_probes_run_in_parallel_not_sequentially(self, mock_boto3):
        """Discrimination test: every probed region sleeps 100ms. Parallel
        wall-clock is ~100ms; sequential would be ~300ms. The 250ms
        assertion catches a regression to sequential probing — comfortably
        above parallel's ceiling, comfortably below sequential's floor."""
        backend, primary = _make_unverified_backend(mock_boto3)
        primary.head_bucket.side_effect = _client_error(403)

        def _slow_200(*_, **__):
            time.sleep(0.1)
            return {}  # HeadBucket returns empty dict on 200

        def _slow_403(*_, **__):
            time.sleep(0.1)
            raise _client_error(403)

        slow_ok = MagicMock()
        slow_ok.head_bucket.side_effect = _slow_200
        slow_403_a = MagicMock()
        slow_403_a.head_bucket.side_effect = _slow_403
        slow_403_b = MagicMock()
        slow_403_b.head_bucket.side_effect = _slow_403

        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3,
            primary=primary,
            per_region={
                "us-west-002": slow_403_a,
                "us-east-005": slow_ok,
                "eu-central-003": slow_403_b,
            },
        )

        start = time.monotonic()
        with pytest.raises(StorageError) as excinfo:
            backend._ensure_region_verified()
        elapsed = time.monotonic() - start

        # us-east-005's 200 wins the classification.
        assert "us-east-005" in str(excinfo.value)
        # Parallel ceiling: ~100ms + small overhead. Sequential floor:
        # ~300ms. 250ms threshold discriminates the two without flakiness.
        assert elapsed < 0.25, (
            f"probe took {elapsed:.3f}s — sequential regression? "
            f"Parallel should complete in ~0.10s; sequential ~0.30s."
        )


# ---------------------------------------------------------------------------
# Probe helper — direct unit test of the dict-returning shape
# ---------------------------------------------------------------------------


class TestProbeHelperDirect:
    def test_returns_one_entry_per_other_region(self, mock_boto3):
        backend, primary = _make_unverified_backend(mock_boto3)
        per_region = {r: MagicMock() for r in ("us-west-002", "us-east-005", "eu-central-003")}
        per_region["us-west-002"].head_bucket.return_value = {}
        per_region["us-east-005"].head_bucket.side_effect = _client_error(404)
        per_region["eu-central-003"].head_bucket.side_effect = _client_error(403)
        mock_boto3.client.side_effect = _dispatch_clients(
            mock_boto3, primary=primary, per_region=per_region
        )

        results = backend._probe_other_b2_regions()

        # Three entries — the user's region (us-west-004) is excluded.
        assert set(results.keys()) == {"us-west-002", "us-east-005", "eu-central-003"}
        assert results["us-west-002"] == 200
        assert results["us-east-005"] == 404
        assert results["eu-central-003"] == 403
