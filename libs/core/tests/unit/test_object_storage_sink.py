"""Tests for ObjectStorageSink."""

from __future__ import annotations

import socket
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import RunStatus, StepStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.storage.base import KeyStrategy, ObjectLockConfig, StorageBackend
from genblaze_core.storage.sink import ObjectStorageSink

# Fake DNS response — resolves to a public IP (bypasses SSRF check)
_FAKE_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class MemoryBackend(StorageBackend):
    """In-memory storage backend for testing."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        # Per-key record of the ExtraArgs dict passed on upload — lets
        # individual tests assert per-upload Cache-Control/checksum policy.
        self.put_extra_args: dict[str, dict] = {}
        self.closed = False

    def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
        self.store[key] = data if isinstance(data, bytes) else data.read()
        self.put_extra_args[key] = dict(extra_args or {})
        return f"https://mem/{key}"

    def get(self, key):
        return self.store[key]

    def exists(self, key):
        return key in self.store

    def delete(self, key):
        self.store.pop(key, None)

    def get_url(self, key, *, expires_in=3600):
        return f"https://mem/{key}"

    def close(self):
        self.closed = True


def _make_run_and_manifest():
    step = Step(
        provider="test",
        model="test-model",
        status=StepStatus.SUCCEEDED,
        assets=[
            Asset(url="https://cdn.example.com/img.png", media_type="image/png"),
        ],
    )
    run = Run(name="test-run", status=RunStatus.COMPLETED, steps=[step])
    manifest = Manifest(run=run)
    manifest.compute_hash()
    return run, manifest


class TestObjectStorageSink:
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_write_run_uploads_manifest(self, mock_urlopen):
        """write_run should upload manifest JSON to storage."""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"img data", b""]
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test")

        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        # Manifest should be uploaded
        manifest_key = f"test/manifests/{run.run_id}.json"
        assert manifest_key in backend.store
        assert manifest.manifest_uri is not None

    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_write_run_sets_manifest_uri(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"data", b""]
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="pfx")

        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        assert manifest.manifest_uri == f"https://mem/pfx/manifests/{run.run_id}.json"

    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_idempotent_manifest_upload(self, mock_urlopen):
        """Second write_run should skip manifest upload if already exists."""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"data", b"", b"data", b""]
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test")

        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)
        initial_store_size = len(backend.store)

        # Second write shouldn't add new entries
        sink.write_run(run, manifest)
        assert len(backend.store) == initial_store_size

    def test_close_closes_backend(self):
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend)
        sink.close()
        assert backend.closed

    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_thread_safety(self, mock_urlopen):
        """Multiple threads calling write_run should not crash."""
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def read_side_effect():
            yield b"data"
            yield b""

        mock_resp.read.side_effect = lambda: b""

        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="mt")

        errors = []

        def worker(i):
            try:
                # Each call uses fresh mocks for read
                mock_resp.read.side_effect = [b"data", b""]
                mock_urlopen.return_value = mock_resp

                run, manifest = _make_run_and_manifest()
                sink.write_run(run, manifest)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_manifest_verifies_after_sink(self, mock_urlopen, _mock_dns):
        """manifest.verify() returns True after sink mutates asset URLs."""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"img data", b""]
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test")

        run, manifest = _make_run_and_manifest()
        original_url = run.steps[0].assets[0].url

        sink.write_run(run, manifest)

        # Asset URL should have been mutated by transfer
        assert run.steps[0].assets[0].url != original_url
        # Manifest hash should still verify (recomputed after mutation)
        assert manifest.verify()

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_manifest_verifies_after_partial_transfer_failure(self, mock_urlopen, _mock_dns):
        """Partial transfer failures must not poison canonical_hash integrity.

        Regression: previously, sink wrote failure IDs into run.metadata
        (which is part of the hashed payload) AFTER compute_hash(), so
        manifest.verify() returned False on any run with failed transfers.
        """
        # urlopen raises on every asset download — forces a failure path
        mock_urlopen.side_effect = RuntimeError("network down")

        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test")

        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        # Sink recorded the failure on the manifest (non-hashed field)
        assert manifest.transfer_failures == [run.steps[0].assets[0].asset_id]
        # Hash must still verify despite the partial failure
        assert manifest.verify()


def _mock_urlopen():
    """Helper: patch urlopen to return fake image data."""
    mock_resp = MagicMock()
    mock_resp.read.side_effect = [b"img data", b""]
    mock_resp.headers = {"Content-Type": "image/png"}
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestObjectStorageSinkHierarchical:
    """HIERARCHICAL layout groups manifest + assets under one run folder."""

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_hierarchical_manifest_grouped_with_run(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test", key_strategy=KeyStrategy.HIERARCHICAL)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        date_str = run.created_at.strftime("%Y-%m-%d")
        expected_key = f"test/runs/{date_str}/{run.run_id}/manifest.json"
        assert expected_key in backend.store

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_hierarchical_assets_grouped_with_run(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test", key_strategy=KeyStrategy.HIERARCHICAL)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        date_str = run.created_at.strftime("%Y-%m-%d")
        asset_id = run.steps[0].assets[0].asset_id
        # Assets live under {prefix}/runs/{date}/{run_id}/assets/
        asset_keys = [k for k in backend.store if "/assets/" in k]
        assert len(asset_keys) == 1
        assert asset_keys[0].startswith(f"test/runs/{date_str}/{run.run_id}/assets/")
        assert asset_id in asset_keys[0]

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_hierarchical_with_tenant(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="test", key_strategy=KeyStrategy.HIERARCHICAL)
        step = Step(
            provider="test",
            model="test-model",
            status=StepStatus.SUCCEEDED,
            assets=[Asset(url="https://cdn.example.com/img.png", media_type="image/png")],
        )
        run = Run(name="test-run", status=RunStatus.COMPLETED, steps=[step], tenant_id="acme")
        manifest = Manifest(run=run)
        manifest.compute_hash()
        sink.write_run(run, manifest)

        date_str = run.created_at.strftime("%Y-%m-%d")
        # Tenant segment appears between runs/ and date
        manifest_key = f"test/runs/acme/{date_str}/{run.run_id}/manifest.json"
        assert manifest_key in backend.store


class TestContentAddressableRegression:
    """CA layout keeps assets and manifests in separate trees (unchanged)."""

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_content_addressable_layout_unchanged(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(
            backend, prefix="pfx", key_strategy=KeyStrategy.CONTENT_ADDRESSABLE
        )
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        # Manifest at {prefix}/manifests/{run_id}.json
        manifest_key = f"pfx/manifests/{run.run_id}.json"
        assert manifest_key in backend.store

        # Assets at {prefix}/assets/{sha[:2]}/{sha[2:4]}/{sha}.ext
        asset_keys = [k for k in backend.store if k != manifest_key]
        assert len(asset_keys) == 1
        assert asset_keys[0].startswith("pfx/assets/")


class TestCacheControlPolicy:
    """Cache-Control headers must match the key-strategy immutability guarantee.

    CAS keys are SHA-256-derived → content is immutable forever → mark public
    + year-long + immutable so Cloudflare/B2 edge can cache aggressively.
    HIERARCHICAL keys are UUID-per-run → shorter private TTL.
    """

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_cas_assets_get_immutable_cache_control(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="p", key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        asset_keys = [k for k in backend.store if k.startswith("p/assets/")]
        assert len(asset_keys) == 1
        assert (
            backend.put_extra_args[asset_keys[0]]["CacheControl"]
            == "public, max-age=31536000, immutable"
        )

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_hierarchical_assets_get_private_short_ttl(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="p", key_strategy=KeyStrategy.HIERARCHICAL)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        asset_keys = [k for k in backend.store if "/assets/" in k]
        assert len(asset_keys) == 1
        assert backend.put_extra_args[asset_keys[0]]["CacheControl"] == "private, max-age=3600"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_cas_manifest_gets_immutable_cache_control(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="p", key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        manifest_key = f"p/manifests/{run.run_id}.json"
        assert (
            backend.put_extra_args[manifest_key]["CacheControl"]
            == "public, max-age=31536000, immutable"
        )

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_hierarchical_manifest_gets_private_short_ttl(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="p", key_strategy=KeyStrategy.HIERARCHICAL)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        date_str = run.created_at.strftime("%Y-%m-%d")
        manifest_key = f"p/runs/{date_str}/{run.run_id}/manifest.json"
        assert backend.put_extra_args[manifest_key]["CacheControl"] == "private, max-age=3600"


class TestManifestObjectLock:
    """Object Lock on manifests — the B2-native provenance retention story."""

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_governance_mode_passes_lock_to_backend(self, mock_urlopen, _mock_dns):
        mock_urlopen.return_value = _mock_urlopen()
        retain_until = datetime(2030, 1, 1, tzinfo=UTC)
        backend = MemoryBackend()
        sink = ObjectStorageSink(
            backend,
            prefix="p",
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            manifest_lock=ObjectLockConfig(retain_until=retain_until, mode="GOVERNANCE"),
        )
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        manifest_key = f"p/manifests/{run.run_id}.json"
        extra = backend.put_extra_args[manifest_key]
        assert extra["ObjectLockMode"] == "GOVERNANCE"
        assert extra["ObjectLockRetainUntilDate"] == retain_until

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_compliance_mode_logs_warning(self, mock_urlopen, _mock_dns, caplog):
        """COMPLIANCE mode is a foot-gun — the sink should log loudly at init."""
        import logging

        mock_urlopen.return_value = _mock_urlopen()
        retain_until = datetime(2030, 1, 1, tzinfo=UTC)
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(
                MemoryBackend(),
                manifest_lock=ObjectLockConfig(retain_until=retain_until, mode="COMPLIANCE"),
            )
        assert any("COMPLIANCE" in rec.message for rec in caplog.records)

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_no_lock_by_default(self, mock_urlopen, _mock_dns):
        """Without explicit manifest_lock, no ObjectLock keys are written."""
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(backend, prefix="p", key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        manifest_key = f"p/manifests/{run.run_id}.json"
        extra = backend.put_extra_args[manifest_key]
        assert "ObjectLockMode" not in extra
        assert "ObjectLockRetainUntilDate" not in extra

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_lock_preserves_cache_control(self, mock_urlopen, _mock_dns):
        """Object Lock and Cache-Control both end up on the same put call."""
        mock_urlopen.return_value = _mock_urlopen()
        backend = MemoryBackend()
        sink = ObjectStorageSink(
            backend,
            prefix="p",
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            manifest_lock=ObjectLockConfig(
                retain_until=datetime.now(UTC) + timedelta(days=365),
            ),
        )
        run, manifest = _make_run_and_manifest()
        sink.write_run(run, manifest)

        manifest_key = f"p/manifests/{run.run_id}.json"
        extra = backend.put_extra_args[manifest_key]
        assert "ObjectLockMode" in extra
        assert "CacheControl" in extra


class TestObjectLockConfig:
    """Direct tests of the ObjectLockConfig dataclass."""

    def test_default_mode_is_governance(self):
        cfg = ObjectLockConfig(retain_until=datetime(2030, 1, 1, tzinfo=UTC))
        assert cfg.mode == "GOVERNANCE"

    def test_to_extra_args_serializes_both_fields(self):
        retain_until = datetime(2030, 1, 1, tzinfo=UTC)
        cfg = ObjectLockConfig(retain_until=retain_until, mode="COMPLIANCE")
        assert cfg.to_extra_args() == {
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": retain_until,
        }

    def test_is_frozen(self):
        """Frozen dataclass — configs shouldn't be mutated after construction."""
        import dataclasses

        cfg = ObjectLockConfig(retain_until=datetime(2030, 1, 1, tzinfo=UTC))
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.mode = "COMPLIANCE"  # type: ignore[misc]

    def test_naive_datetime_rejected(self):
        """Naive datetimes are rejected — S3 treats them ambiguously and we
        refuse to silently accept multi-year retention with a wrong anchor."""
        with pytest.raises(ValueError, match="timezone-aware"):
            ObjectLockConfig(retain_until=datetime(2030, 1, 1))

    def test_past_retention_warns_but_allows(self, caplog):
        """A past retain_until uploads effectively-unlocked; we warn loudly
        but don't block (allows migration / testing workflows)."""
        import logging

        past = datetime(2000, 1, 1, tzinfo=UTC)
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.object_lock"):
            ObjectLockConfig(retain_until=past)
        assert any("in the past" in rec.message for rec in caplog.records)
