"""Tests for AssetTransfer — download, hash, upload flow."""

from __future__ import annotations

import socket
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import StorageError
from genblaze_core.models.asset import Asset
from genblaze_core.storage.base import KeyStrategy, StorageBackend
from genblaze_core.storage.transfer import (
    AssetTransfer,
    _build_key,
    _read_local_file,
    _validate_url,
)

# Fake DNS response for test hostnames — resolves to a public IP
_FAKE_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class FakeBackend(StorageBackend):
    """In-memory storage backend for testing."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
        if isinstance(data, bytes):
            self.store[key] = data
        else:
            self.store[key] = data.read()
        return f"https://storage.example.com/{key}"

    def get(self, key):
        return self.store[key]

    def exists(self, key):
        return key in self.store

    def delete(self, key):
        self.store.pop(key, None)

    def get_url(self, key, *, expires_in=3600):
        return f"https://storage.example.com/{key}"


class TestValidateUrl:
    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    def test_https_allowed(self, _mock_dns):
        _validate_url("https://cdn.example.com/img.png")

    def test_http_rejected(self):
        with pytest.raises(StorageError, match="Only HTTPS"):
            _validate_url("http://cdn.example.com/img.png")

    def test_file_rejected(self):
        with pytest.raises(StorageError, match="Only HTTPS"):
            _validate_url("file:///etc/passwd")

    def test_localhost_rejected(self):
        with pytest.raises(StorageError, match="Private/loopback"):
            _validate_url("https://localhost/img.png")

    def test_private_ip_rejected(self):
        """Private IPs (resolved via DNS) are blocked."""
        private_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=private_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                _validate_url("https://internal.example.com/img.png")

    def test_172_private_ip_rejected(self):
        """172.16.x.x range is blocked."""
        private_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.16.0.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=private_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                _validate_url("https://internal.example.com/img.png")

    def test_imds_ip_rejected(self):
        """169.254.x.x (IMDS/link-local) is blocked."""
        imds_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=imds_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                _validate_url("https://metadata.example.com/latest")

    def test_cgn_ip_rejected(self):
        """100.64.x.x (Carrier-grade NAT) is blocked."""
        cgn_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=cgn_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                _validate_url("https://cgn.example.com/img.png")

    def test_loopback_ip_rejected(self):
        """127.0.0.1 is blocked."""
        lo_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=lo_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                _validate_url("https://sneaky.example.com/img.png")

    def test_unresolvable_host_rejected(self):
        """Unresolvable hostnames are rejected."""
        with patch(
            "genblaze_core._utils.socket.getaddrinfo",
            side_effect=socket.gaierror("Name not found"),
        ):
            with pytest.raises(StorageError, match="Cannot resolve"):
                _validate_url("https://doesnotexist.invalid/img.png")


class TestBuildKey:
    def test_content_addressable(self):
        asset = Asset(url="https://x.com/img.png", media_type="image/png")
        key = _build_key(KeyStrategy.CONTENT_ADDRESSABLE, "assets", asset, "abcdef1234", ".png")
        assert key == "assets/ab/cd/abcdef1234.png"

    def test_hierarchical(self):
        asset = Asset(url="https://x.com/img.png", media_type="image/png")
        key = _build_key(
            KeyStrategy.HIERARCHICAL,
            "assets",
            asset,
            "abcdef1234",
            ".png",
            tenant="acme",
            date_str="2026-03-11",
            run_id="run-123",
        )
        assert key == f"assets/acme/2026-03-11/run-123/assets/{asset.asset_id}.png"

    def test_hierarchical_no_tenant(self):
        """Tenant segment is omitted when None."""
        asset = Asset(url="https://x.com/img.png", media_type="image/png")
        key = _build_key(
            KeyStrategy.HIERARCHICAL,
            "pfx",
            asset,
            "abcdef1234",
            ".png",
            tenant=None,
            date_str="2026-03-11",
            run_id="run-123",
        )
        assert key == f"pfx/2026-03-11/run-123/assets/{asset.asset_id}.png"
        assert "None" not in key


class TestAssetTransfer:
    def _make_transfer(self, backend=None):
        backend = backend or FakeBackend()
        return AssetTransfer(backend, prefix="assets"), backend

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_transfer_updates_asset(self, mock_urlopen, _mock_dns):
        """Transfer should set sha256, size_bytes, and url on the asset."""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"fake image data", b""]
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        transfer, backend = self._make_transfer()
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        key = transfer.transfer(asset)

        assert asset.sha256 is not None
        assert len(asset.sha256) == 64
        assert asset.size_bytes == len(b"fake image data")
        assert "storage.example.com" in asset.url
        assert key in backend.store

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_content_addressable_dedup(self, mock_urlopen, _mock_dns):
        """Content-addressable uploads skip if key already exists."""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"data", b""]
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        backend = FakeBackend()
        transfer = AssetTransfer(backend, prefix="assets")
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        # First transfer
        key = transfer.transfer(asset)
        assert key in backend.store

        # Second transfer with same data — should skip put
        mock_resp.read.side_effect = [b"data", b""]
        mock_urlopen.return_value = mock_resp
        asset2 = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        original_put = backend.put
        put_called = []

        def tracking_put(*args, **kwargs):
            put_called.append(True)
            return original_put(*args, **kwargs)

        backend.put = tracking_put

        transfer.transfer(asset2)
        assert len(put_called) == 0  # Should have skipped

    def test_http_url_rejected(self):
        transfer, _ = self._make_transfer()
        asset = Asset(url="http://insecure.example.com/img.png", media_type="image/png")
        with pytest.raises(StorageError, match="Only HTTPS"):
            transfer.transfer(asset)

    def test_file_url_transfer(self, tmp_path):
        """file:// URLs are read directly from disk."""
        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"local image data")
        transfer, backend = self._make_transfer()
        asset = Asset(url=f"file://{test_file}", media_type="image/png")
        key = transfer.transfer(asset)
        assert asset.sha256 is not None
        assert asset.size_bytes == len(b"local image data")
        assert key in backend.store

    def test_file_url_outside_allowed_dirs_rejected(self, tmp_path):
        """file:// URLs outside temp/allowed dirs are blocked."""
        fake_path = Path("/Users/sensitive/secret.png")
        transfer, _ = self._make_transfer()
        asset = Asset(url=f"file://{fake_path}", media_type="image/png")
        with pytest.raises(StorageError, match="outside allowed directories"):
            transfer.transfer(asset)

    def test_file_url_with_extra_roots_allowed(self, tmp_path):
        """file:// URLs under extra_roots are allowed."""
        custom_dir = tmp_path / "custom_output"
        custom_dir.mkdir()
        test_file = custom_dir / "asset.png"
        test_file.write_bytes(b"custom data")

        backend = FakeBackend()
        transfer = AssetTransfer(backend, prefix="assets", allowed_roots=[custom_dir])
        asset = Asset(url=f"file://{test_file}", media_type="image/png")
        key = transfer.transfer(asset)
        assert asset.sha256 is not None
        assert key in backend.store

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_transfer_streams_to_file_object(self, mock_urlopen, _mock_dns):
        """Remote transfers pass a file-like object to backend.put(), not bytes."""
        # Simulate a response larger than _SPOOL_THRESHOLD to test disk spooling
        large_data = b"x" * (1024 * 1024 + 1)  # Just over 1MB
        mock_resp = MagicMock()
        # Return data in chunks, then empty
        chunk_size = 256 * 1024
        chunks = [large_data[i : i + chunk_size] for i in range(0, len(large_data), chunk_size)]
        chunks.append(b"")
        mock_resp.read.side_effect = chunks
        mock_resp.headers = {"Content-Type": "video/mp4"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # Track what backend.put receives
        received_data = []

        class TrackingBackend(FakeBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                received_data.append(type(data).__name__)
                # Read file-like object for storage
                if hasattr(data, "read"):
                    content = data.read()
                else:
                    content = data
                self.store[key] = content
                return f"https://storage.example.com/{key}"

        backend = TrackingBackend()
        transfer = AssetTransfer(backend, prefix="assets")
        asset = Asset(url="https://cdn.example.com/video.mp4", media_type="video/mp4")

        transfer.transfer(asset)

        assert asset.sha256 is not None
        assert asset.size_bytes == len(large_data)
        # Verify backend received a file-like object, not raw bytes
        assert received_data[0] == "SpooledTemporaryFile"

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_download_timeout_raises_storage_error(self, mock_urlopen, _mock_dns):
        """urlopen timeout propagates as StorageError."""
        mock_urlopen.side_effect = urllib.error.URLError("timed out")
        transfer, _ = self._make_transfer()
        asset = Asset(url="https://slow.example.com/huge.mp4", media_type="video/mp4")
        with pytest.raises(StorageError, match="Failed to download"):
            transfer.transfer(asset)

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer.urllib.request.urlopen")
    def test_oversized_download_aborted(self, mock_urlopen, _mock_dns):
        """Downloads exceeding max_download_bytes are rejected."""
        # Return chunks that exceed the small limit
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"x" * 1024, b"x" * 1024, b""]
        mock_resp.headers = {"Content-Type": "video/mp4"}
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        backend = FakeBackend()
        # Set a very small limit to trigger the guard
        transfer = AssetTransfer(backend, prefix="assets", max_download_bytes=1000)
        asset = Asset(url="https://cdn.example.com/huge.mp4", media_type="video/mp4")
        with pytest.raises(StorageError, match="exceeds.*byte limit"):
            transfer.transfer(asset)


class TestReadLocalFile:
    def test_symlink_escape_rejected(self, tmp_path, monkeypatch):
        """Symlinks that resolve outside allowed dirs are rejected."""
        # Restrict ALLOWED_FILE_ROOTS to only a subdirectory, not the whole tmp
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive data")
        # Symlink inside allowed dir points to file outside it
        link = allowed / "escape.txt"
        link.symlink_to(secret)
        # Patch ALLOWED_FILE_ROOTS so tmp_path itself is not allowed
        monkeypatch.setattr("genblaze_core.storage.transfer.ALLOWED_FILE_ROOTS", (allowed,))
        with pytest.raises(StorageError, match="outside allowed directories"):
            _read_local_file(f"file://{link}")

    def test_extra_roots_allows_access(self, tmp_path):
        """Files under extra_roots are accessible."""
        custom_dir = tmp_path / "output"
        custom_dir.mkdir()
        f = custom_dir / "asset.png"
        f.write_bytes(b"png data")
        data, _ = _read_local_file(f"file://{f}", extra_roots=[custom_dir])
        assert data == b"png data"

    def test_nonexistent_file_raises(self, tmp_path):
        """Reading a missing file raises StorageError."""
        missing = tmp_path / "nope.png"
        with pytest.raises(StorageError, match="Failed to read"):
            _read_local_file(f"file://{missing}")
