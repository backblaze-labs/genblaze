"""Tests for ObjectStorageSink."""

from __future__ import annotations

import socket
import threading
from unittest.mock import MagicMock, patch

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import RunStatus, StepStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.storage.base import KeyStrategy, StorageBackend
from genblaze_core.storage.sink import ObjectStorageSink

# Fake DNS response — resolves to a public IP (bypasses SSRF check)
_FAKE_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class MemoryBackend(StorageBackend):
    """In-memory storage backend for testing."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.closed = False

    def put(self, key, data, *, content_type=None, metadata=None):
        self.store[key] = data if isinstance(data, bytes) else data.read()
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
