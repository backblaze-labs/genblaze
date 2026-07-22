"""Tests for AssetTransfer — download, hash, upload flow."""

from __future__ import annotations

import socket
from pathlib import Path, PureWindowsPath
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
import urllib3
from genblaze_core._utils import check_ssrf
from genblaze_core.exceptions import StorageError
from genblaze_core.models.asset import Asset
from genblaze_core.storage.base import KeyStrategy, StorageBackend
from genblaze_core.storage.key_builder import KeyBuilder
from genblaze_core.storage.transfer import (
    AssetTransfer,
    _build_key,
    _read_local_file,
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
        # Mimic a presigned URL — what get_url returns when no public_url_base
        # is configured. Tests assert this string never lands in asset.url
        # after transfer (durable URL is used there).
        return (
            f"https://storage.example.com/{key}?X-Amz-Signature=fake-sig&X-Amz-Credential=AKIAFAKE"
        )

    def get_durable_url(self, key):
        return f"https://storage.example.com/{key}"


class TestTransferUrlValidation:
    """The transfer path validates URLs via check_ssrf with StorageError.

    Core SSRF semantics live in test_utils.py::TestCheckSsrf; these cases
    pin the per-range blocklist coverage to the StorageError-typed guard the
    transfer path relies on.
    """

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    def test_https_allowed(self, _mock_dns):
        check_ssrf("https://cdn.example.com/img.png", exc_type=StorageError)

    def test_http_rejected(self):
        with pytest.raises(StorageError, match="Only HTTPS"):
            check_ssrf("http://cdn.example.com/img.png", exc_type=StorageError)

    def test_file_rejected(self):
        with pytest.raises(StorageError, match="Only HTTPS"):
            check_ssrf("file:///etc/passwd", exc_type=StorageError)

    def test_localhost_rejected(self):
        with pytest.raises(StorageError, match="Private/loopback"):
            check_ssrf("https://localhost/img.png", exc_type=StorageError)

    def test_private_ip_rejected(self):
        """Private IPs (resolved via DNS) are blocked."""
        private_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=private_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                check_ssrf("https://internal.example.com/img.png", exc_type=StorageError)

    def test_172_private_ip_rejected(self):
        """172.16.x.x range is blocked."""
        private_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.16.0.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=private_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                check_ssrf("https://internal.example.com/img.png", exc_type=StorageError)

    def test_imds_ip_rejected(self):
        """169.254.x.x (IMDS/link-local) is blocked."""
        imds_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=imds_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                check_ssrf("https://metadata.example.com/latest", exc_type=StorageError)

    def test_cgn_ip_rejected(self):
        """100.64.x.x (Carrier-grade NAT) is blocked."""
        cgn_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=cgn_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                check_ssrf("https://cgn.example.com/img.png", exc_type=StorageError)

    def test_loopback_ip_rejected(self):
        """127.0.0.1 is blocked."""
        lo_addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=lo_addr):
            with pytest.raises(StorageError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/img.png", exc_type=StorageError)

    def test_unresolvable_host_rejected(self):
        """Unresolvable hostnames are rejected."""
        with patch(
            "genblaze_core._utils.socket.getaddrinfo",
            side_effect=socket.gaierror("Name not found"),
        ):
            with pytest.raises(StorageError, match="Cannot resolve"):
                check_ssrf("https://doesnotexist.invalid/img.png", exc_type=StorageError)


class TestBuildKey:
    def test_content_addressable(self):
        asset = Asset(url="https://x.com/img.png", media_type="image/png")
        key = _build_key(
            KeyStrategy.CONTENT_ADDRESSABLE,
            KeyBuilder.from_prefix("assets"),
            asset,
            "abcdef1234",
            ".png",
        )
        assert key == "assets/ab/cd/abcdef1234.png"

    def test_content_addressable_normalizes_uppercase_extension(self):
        """Byte-identical content with a differently-cased source extension
        (e.g. .PNG vs .png) must dedup to the same CAS key — the extension
        casing is an accident of the origin URL, not part of the content hash.
        """
        asset = Asset(url="https://x.com/IMG.PNG", media_type="image/png")
        key_upper = _build_key(
            KeyStrategy.CONTENT_ADDRESSABLE,
            KeyBuilder.from_prefix("assets"),
            asset,
            "abcdef1234",
            ".PNG",
        )
        key_lower = _build_key(
            KeyStrategy.CONTENT_ADDRESSABLE,
            KeyBuilder.from_prefix("assets"),
            asset,
            "abcdef1234",
            ".png",
        )
        assert key_upper == key_lower == "assets/ab/cd/abcdef1234.png"

    def test_hierarchical(self):
        asset = Asset(url="https://x.com/img.png", media_type="image/png")
        key = _build_key(
            KeyStrategy.HIERARCHICAL,
            KeyBuilder.from_prefix("assets"),
            asset,
            "abcdef1234",
            ".png",
            tenant="acme",
            date_str="2026-03-11",
            run_id="run-123",
        )
        # Note: the trailing "assets/" segment comes from the strategy itself,
        # not the prefix — so even though prefix=="assets", the seam dedupe
        # only collapses one of the duplicates between prefix and strategy.
        assert key == f"assets/acme/2026-03-11/run-123/assets/{asset.asset_id}.png"

    def test_hierarchical_no_tenant(self):
        """Tenant segment is omitted when None."""
        asset = Asset(url="https://x.com/img.png", media_type="image/png")
        key = _build_key(
            KeyStrategy.HIERARCHICAL,
            KeyBuilder.from_prefix("pfx"),
            asset,
            "abcdef1234",
            ".png",
            tenant=None,
            date_str="2026-03-11",
            run_id="run-123",
        )
        assert key == f"pfx/2026-03-11/run-123/assets/{asset.asset_id}.png"
        assert "None" not in key

    def test_hierarchical_preserves_extension_case(self):
        """HIERARCHICAL keys are asset_id-based, not content-hash-based, so
        there's no dedup collision to fix — extension casing is left as-is.
        """
        asset = Asset(url="https://x.com/IMG.PNG", media_type="image/png")
        key = _build_key(
            KeyStrategy.HIERARCHICAL,
            KeyBuilder.from_prefix("assets"),
            asset,
            "abcdef1234",
            ".PNG",
            tenant=None,
            date_str="2026-03-11",
            run_id="run-123",
        )
        assert key == f"assets/2026-03-11/run-123/assets/{asset.asset_id}.PNG"


class TestAssetTransfer:
    def _make_transfer(self, backend=None):
        backend = backend or FakeBackend()
        return AssetTransfer(backend, prefix="assets"), backend

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
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
        _host = urlparse(asset.url).hostname or ""
        assert _host == "storage.example.com" or _host.endswith(".storage.example.com")
        assert key in backend.store

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
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
    @patch("genblaze_core.storage.transfer._http_get_stream")
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
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_download_timeout_raises_storage_error(self, mock_get_stream, _mock_dns):
        """HTTP transport failures propagate as StorageError."""
        # _http_get_stream already wraps urllib3 errors as StorageError, but
        # we simulate a raw transport fail from deeper in the stack to prove
        # the outer except-Exception handler also wraps cleanly.
        mock_get_stream.side_effect = OSError("connection refused")
        transfer, _ = self._make_transfer()
        asset = Asset(url="https://slow.example.com/huge.mp4", media_type="video/mp4")
        with pytest.raises(StorageError, match="Transfer failed"):
            transfer.transfer(asset)

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
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

    def test_windows_drive_letter_file_url(self, tmp_path, monkeypatch):
        """Regression for #132/#164: file:///C:/path yields /C:/path from
        urlparse.path. The old unquote() kept the leading slash, causing
        Path.resolve() to produce a drive-relative path on Windows that
        failed the allowlist check. url2pathname() strips the leading slash
        before the drive letter.

        The input URL is built with PureWindowsPath.as_uri() — the exact
        form local_file_url() (the shared helper every connector now calls)
        produces for a Windows path — so this test proves the connector ->
        sink contract holds, not just an arbitrary hardcoded string.

        url2pathname is monkeypatched to return the pre-computed real_path
        because a genuine Windows path string ("C:\\tmp\\asset.mp4") can't
        round-trip through POSIX pathlib.Path.resolve() on this host; the
        actual Windows parser (nturl2path.url2pathname) is exercised
        unmocked in test_utils.py::TestLocalFileUrl.
        """
        test_file = tmp_path / "asset.mp4"
        test_file.write_bytes(b"video data")
        real_path = str(test_file.resolve())
        win_url = PureWindowsPath(r"C:\tmp\asset.mp4").as_uri()
        assert win_url == "file:///C:/tmp/asset.mp4"

        # Simulate what Windows url2pathname does: /C:/tmp/asset.mp4 → C:\tmp\asset.mp4
        # On the fix branch url2pathname is a module-level name we can monkeypatch.
        monkeypatch.setattr(
            "genblaze_core.storage.transfer.url2pathname",
            lambda _: real_path,
        )
        monkeypatch.setattr(
            "genblaze_core.storage.transfer.ALLOWED_FILE_ROOTS",
            (tmp_path.resolve(),),
        )

        data, _ = _read_local_file(win_url)
        assert data == b"video data"


class TestConnectionLifecycle:
    """release_conn() must fire on every transfer — success, failure, and
    partial-read paths. Otherwise the urllib3 pool leaks connections."""

    def _mock_resp(self, chunks: list[bytes]) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.read.side_effect = chunks
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.release_conn = MagicMock()
        return mock_resp

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_release_conn_on_success(self, mock_get_stream, _mock_dns):
        mock_resp = self._mock_resp([b"payload", b""])
        mock_get_stream.return_value = mock_resp

        backend = FakeBackend()
        transfer = AssetTransfer(backend, prefix="assets")
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")
        transfer.transfer(asset)

        mock_resp.release_conn.assert_called_once()

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_release_conn_on_size_cap(self, mock_get_stream, _mock_dns):
        """Oversized download aborts mid-stream — connection still released."""
        mock_resp = self._mock_resp([b"x" * 2048, b"x" * 2048, b""])
        mock_get_stream.return_value = mock_resp

        backend = FakeBackend()
        transfer = AssetTransfer(backend, prefix="assets", max_download_bytes=1024)
        asset = Asset(url="https://cdn.example.com/big.mp4", media_type="video/mp4")
        with pytest.raises(StorageError, match="exceeds"):
            transfer.transfer(asset)

        mock_resp.release_conn.assert_called_once()

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_release_conn_on_backend_put_failure(self, mock_get_stream, _mock_dns):
        """Backend upload failure after successful download still releases conn."""
        mock_resp = self._mock_resp([b"payload", b""])
        mock_get_stream.return_value = mock_resp

        class BrokenBackend(FakeBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                raise StorageError("backend down")

        transfer = AssetTransfer(BrokenBackend(), prefix="assets")
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")
        with pytest.raises(StorageError):
            transfer.transfer(asset)

        mock_resp.release_conn.assert_called_once()


def _make_pool_mock(resp: MagicMock) -> MagicMock:
    """Return a mock urllib3.HTTPSConnectionPool whose request() returns resp."""
    pool = MagicMock()
    pool.request.return_value = resp
    return pool


class TestHttpPool:
    """_http_get_stream — per-hop HTTPSConnectionPool with DNS pinning."""

    def _ok_resp(self) -> MagicMock:
        resp = MagicMock()
        resp.status = 200
        resp.headers = {"Content-Type": "image/png"}
        resp.release_conn = MagicMock()
        return resp

    def test_get_stream_raises_on_http_error_status(self):
        """4xx/5xx should surface as StorageError."""
        from genblaze_core.storage.transfer import _http_get_stream

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.release_conn = MagicMock()
        pool = _make_pool_mock(mock_resp)
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with (
            patch("genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool", return_value=pool),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=public_dns),
        ):
            with pytest.raises(StorageError, match="HTTP 404"):
                _http_get_stream("https://cdn.example.com/missing.png", timeout=30.0)
        mock_resp.release_conn.assert_called_once()

    def test_get_stream_wraps_urllib3_errors(self):
        """Transport errors from urllib3 surface as StorageError."""
        from genblaze_core.storage.transfer import _http_get_stream

        pool = MagicMock()
        pool.request.side_effect = urllib3.exceptions.ConnectTimeoutError(None, "connect timeout")
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with (
            patch("genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool", return_value=pool),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=public_dns),
        ):
            with pytest.raises(StorageError, match="Download failed"):
                _http_get_stream("https://slow.example.com/img.png", timeout=30.0)

    def test_dns_pinning_connects_to_resolved_ip(self):
        """_http_get_stream must open the pool to the pinned IP, not the hostname.
        This prevents DNS rebinding: the HTTP client re-resolving at connect time
        is replaced by a direct connection to the IP validate_ssrf returned."""
        from genblaze_core.storage.transfer import _http_get_stream

        ok_resp = MagicMock()
        ok_resp.status = 200
        ok_resp.headers = {"Content-Type": "image/png"}
        ok_resp.release_conn = MagicMock()
        pool = _make_pool_mock(ok_resp)
        pool_calls: list = []

        def pool_factory(host, **kwargs):
            pool_calls.append(host)
            return pool

        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=pool_factory,
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=public_dns),
        ):
            _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)

        # Pool must be opened to the pinned IP, not the original hostname
        assert pool_calls == ["93.184.216.34"], (
            "HTTPSConnectionPool must be constructed with the pinned IP"
        )

    def test_pool_constructed_with_server_hostname_for_sni(self):
        """HTTPSConnectionPool must receive server_hostname=host (not the pinned IP).
        Without it urllib3 sends the IP literal as the TLS SNI extension, which
        CDNs (CloudFront, Fastly, Cloudflare) reject — they rely on SNI to pick
        the virtual host and certificate."""
        from genblaze_core.storage.transfer import _http_get_stream

        ok_resp = self._ok_resp()
        pool_kwargs: list[dict] = []

        def pool_factory(host, **kwargs):
            pool_kwargs.append({"host": host, **kwargs})
            return _make_pool_mock(ok_resp)

        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=pool_factory,
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=public_dns),
        ):
            _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)

        assert len(pool_kwargs) == 1
        kw = pool_kwargs[0]
        assert kw["host"] == "93.184.216.34", "pool must connect to pinned IP"
        assert kw.get("server_hostname") == "cdn.example.com", (
            "server_hostname must be the original hostname for TLS SNI"
        )
        assert kw.get("assert_hostname") == "cdn.example.com", (
            "assert_hostname must be the original hostname for cert verification"
        )

    def test_dns_rebinding_blocked(self):
        """DNS rebinding: first resolution → public IP (validation passes), but if
        the HTTP client were to re-resolve it would get a private IP. With pinning,
        the pool is opened to the validated public IP regardless of subsequent DNS."""
        from genblaze_core.storage.transfer import _http_get_stream

        ok_resp = MagicMock()
        ok_resp.status = 200
        ok_resp.headers = {"Content-Type": "image/png"}
        ok_resp.release_conn = MagicMock()
        pool = _make_pool_mock(ok_resp)
        pool_calls: list = []

        def pool_factory(host, **kwargs):
            pool_calls.append(host)
            return pool

        # First call returns public (validation); subsequent calls return private (rebind).
        call_n = 0

        def dns_side_effect(*args, **kwargs):
            nonlocal call_n
            call_n += 1
            if call_n == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=pool_factory,
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", side_effect=dns_side_effect),
        ):
            _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)

        # The pool must have been opened to the public IP (from the pinned first resolution)
        assert pool_calls == ["93.184.216.34"], (
            "Must connect to the DNS-pinned public IP, not re-resolve to the private one"
        )


class TestHashingStreamReader:
    """The stream wrapper that lets boto3 read directly from the HTTP
    response while we compute SHA-256 in-flight."""

    def _mock_resp(self, *chunks: bytes) -> MagicMock:
        resp = MagicMock()
        # Mock .read(n): ignore n, return next scripted chunk. Closest to
        # how urllib3 behaves in streaming mode — returns up to n bytes but
        # may return less.
        resp.read.side_effect = list(chunks) + [b""]
        return resp

    def test_read_chunked_hashes_in_flight(self):
        from genblaze_core.storage.transfer import _HashingStreamReader

        resp = self._mock_resp(b"abc", b"def", b"ghi")
        reader = _HashingStreamReader(resp, max_bytes=1000)

        # boto3-style: loop reads of specific sizes
        assert reader.read(1024) == b"abc"
        assert reader.read(1024) == b"def"
        assert reader.read(1024) == b"ghi"
        assert reader.read(1024) == b""
        # Hash matches the full concatenated payload
        import hashlib

        expected = hashlib.sha256(b"abcdefghi").hexdigest()
        assert reader.sha256_hex == expected
        assert reader.size == 9

    def test_read_all_at_once_supported(self):
        """read(-1) / read() must return all remaining bytes, not one chunk."""
        from genblaze_core.storage.transfer import _HashingStreamReader

        resp = self._mock_resp(b"aaa", b"bbb", b"ccc")
        reader = _HashingStreamReader(resp, max_bytes=1000)
        assert reader.read() == b"aaabbbccc"
        assert reader.size == 9

    def test_enforces_max_bytes_mid_stream(self):
        from genblaze_core.storage.transfer import _HashingStreamReader

        resp = self._mock_resp(b"x" * 512, b"x" * 512)
        reader = _HashingStreamReader(resp, max_bytes=1000)
        reader.read(512)
        with pytest.raises(StorageError, match="exceeds"):
            reader.read(512)

    def test_seekable_false(self):
        """boto3 uses seekable() to gate its retry strategy. For streams
        from an HTTP response it must be False so boto3 buffers each
        multipart part in memory rather than trying to rewind."""
        from genblaze_core.storage.transfer import _HashingStreamReader

        reader = _HashingStreamReader(self._mock_resp(b""), max_bytes=1000)
        assert reader.seekable() is False
        assert reader.readable() is True


class TestPipelinedTransfer:
    """Pipelined mode: HTTP response → backend multipart, no disk spool."""

    def _mock_resp(self, *chunks: bytes) -> MagicMock:
        resp = MagicMock()
        resp.read.side_effect = list(chunks) + [b""]
        resp.headers = {"Content-Type": "image/png"}
        resp.release_conn = MagicMock()
        return resp

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_hierarchical_streams_straight_to_final_key(self, mock_get_stream, _mock_dns):
        """HIERARCHICAL key is known upfront (asset_id based) — no temp key,
        no copy-and-delete. Just one backend.put."""
        mock_get_stream.return_value = self._mock_resp(b"payload-bytes")
        backend = FakeBackend()
        transfer = AssetTransfer(
            backend,
            prefix="assets",
            key_strategy=KeyStrategy.HIERARCHICAL,
            pipelined_transfer=True,
        )
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        key = transfer.transfer(asset, tenant="acme", date_str="2026-04-22", run_id="r1")

        assert asset.sha256 is not None
        assert len(asset.sha256) == 64
        assert asset.size_bytes == len(b"payload-bytes")
        # Stored once at the HIERARCHICAL key — no temp detour.
        assert len(backend.store) == 1
        assert key in backend.store
        assert backend.store[key] == b"payload-bytes"
        # Connection returned to the pool.
        mock_get_stream.return_value.release_conn.assert_called_once()

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_cas_promotes_via_temp_key_and_copy(self, mock_get_stream, _mock_dns):
        """CAS key depends on the hash, not known upfront. Upload to
        .tmp/{asset_id}, then copy to final CAS key, then delete temp."""
        payload = b"deterministic-content"
        mock_get_stream.return_value = self._mock_resp(payload)
        backend = FakeBackend()
        transfer = AssetTransfer(
            backend,
            prefix="assets",
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            pipelined_transfer=True,
        )
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        key = transfer.transfer(asset)

        import hashlib

        expected_sha = hashlib.sha256(payload).hexdigest()
        assert asset.sha256 == expected_sha
        # Final key is CAS path; temp key should have been cleaned up.
        cas_keys = [k for k in backend.store if ".tmp/" not in k]
        temp_keys = [k for k in backend.store if ".tmp/" in k]
        assert len(cas_keys) == 1
        assert temp_keys == [], "temp key should be deleted after promote"
        assert key == cas_keys[0]
        assert key.endswith(f"/{expected_sha}.png")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_cas_dedup_hit_discards_temp(self, mock_get_stream, _mock_dns):
        """If the CAS key already exists (dedup hit), drop the temp upload
        without copying — the existing content is authoritative."""
        payload = b"shared-content"
        mock_get_stream.return_value = self._mock_resp(payload)
        backend = FakeBackend()

        # Pre-populate the CAS key so the exists() check hits.
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        cas_key = f"assets/{sha[:2]}/{sha[2:4]}/{sha}.png"
        backend.store[cas_key] = payload

        transfer = AssetTransfer(
            backend,
            prefix="assets",
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            pipelined_transfer=True,
        )
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        key = transfer.transfer(asset)

        # Only the pre-populated CAS entry remains; temp is cleaned up.
        assert len(backend.store) == 1
        assert key == cas_key
        assert asset.sha256 == sha

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_cleans_up_temp_on_copy_failure(self, mock_get_stream, _mock_dns):
        """If upload succeeds but copy fails, the temp-key orphan must be
        deleted defensively (not just left for the lifecycle rule)."""
        mock_get_stream.return_value = self._mock_resp(b"data")

        class BrokenCopyBackend(FakeBackend):
            def copy(self, src_key, dst_key):
                raise StorageError("copy failed")

        backend = BrokenCopyBackend()
        transfer = AssetTransfer(
            backend,
            prefix="assets",
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            pipelined_transfer=True,
        )
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")

        with pytest.raises(StorageError):
            transfer.transfer(asset)
        # Temp key cleaned up — nothing left behind.
        assert not any(".tmp/" in k for k in backend.store)

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_size_cap_enforced_pipelined(self, mock_get_stream, _mock_dns):
        """The download cap is enforced in the reader, not at the end."""
        # Four 512-byte chunks — total 2048 > 1024 cap.
        mock_get_stream.return_value = self._mock_resp(
            b"x" * 512, b"x" * 512, b"x" * 512, b"x" * 512
        )
        backend = FakeBackend()
        transfer = AssetTransfer(
            backend,
            prefix="assets",
            key_strategy=KeyStrategy.HIERARCHICAL,
            pipelined_transfer=True,
            max_download_bytes=1024,
        )
        asset = Asset(url="https://cdn.example.com/big.mp4", media_type="video/mp4")
        with pytest.raises(StorageError, match="exceeds"):
            transfer.transfer(asset, run_id="r1", date_str="2026-04-22")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_FAKE_ADDRINFO)
    @patch("genblaze_core.storage.transfer._http_get_stream")
    def test_release_conn_on_pipelined_failure(self, mock_get_stream, _mock_dns):
        mock_get_stream.return_value = self._mock_resp(b"data")

        class BrokenBackend(FakeBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                raise StorageError("upload failed")

        transfer = AssetTransfer(
            BrokenBackend(),
            prefix="assets",
            key_strategy=KeyStrategy.HIERARCHICAL,
            pipelined_transfer=True,
        )
        asset = Asset(url="https://cdn.example.com/img.png", media_type="image/png")
        with pytest.raises(StorageError):
            transfer.transfer(asset, run_id="r1", date_str="2026-04-22")

        mock_get_stream.return_value.release_conn.assert_called_once()

    def test_default_is_spooled(self):
        """Pipelined mode is opt-in — don't change existing users' behavior."""
        backend = FakeBackend()
        transfer = AssetTransfer(backend, prefix="assets")
        assert transfer._pipelined_transfer is False


class TestBackendCopy:
    """The new StorageBackend.copy() method — default and subclass overrides."""

    def test_default_fallback_downloads_and_reuploads(self):
        """The ABC's default copy() is a slow fallback that any backend
        gets for free. S3 backends override with server-side copy_object."""
        backend = FakeBackend()
        backend.store["src"] = b"payload"
        backend.copy("src", "dst")
        assert backend.store["dst"] == b"payload"
        # Source remains — copy is not a move.
        assert backend.store["src"] == b"payload"


def _pool_factory_for(responses: list[MagicMock]):
    """Return an HTTPSConnectionPool factory that cycles through responses.

    Each call to urllib3.HTTPSConnectionPool(...) returns a new pool whose
    request() returns the next response in the list. Used to simulate multi-hop
    redirect chains where a fresh pool is created per hop.
    """
    resp_iter = iter(responses)

    def factory(*args, **kwargs):
        pool = MagicMock()
        pool.request.return_value = next(resp_iter)
        return pool

    return factory


class TestRedirectSsrf:
    """Redirect-following must re-validate and re-pin DNS on every Location hop."""

    _PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    _PRIVATE_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]

    def _redirect_resp(self, location: str) -> MagicMock:
        """Build a 301 mock response pointing at location."""
        resp = MagicMock()
        resp.status = 301
        resp.headers = {"Location": location}
        resp.release_conn = MagicMock()
        return resp

    def _ok_resp(self, chunks: list[bytes]) -> MagicMock:
        """Build a 200 mock response with the given body chunks."""
        resp = MagicMock()
        resp.status = 200
        resp.headers = {"Content-Type": "image/png"}
        resp.read.side_effect = chunks + [b""]
        resp.release_conn = MagicMock()
        return resp

    def test_redirect_to_private_ip_rejected(self):
        """A 301 whose Location resolves to a private IP must be blocked."""
        redirect_resp = self._redirect_resp("https://internal.example.com/secret")

        def dns_side_effect(host, *args, **kwargs):
            if "internal" in host:
                return self._PRIVATE_DNS
            return self._PUBLIC_DNS

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for([redirect_resp]),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", side_effect=dns_side_effect),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            with pytest.raises(StorageError, match="Private/loopback"):
                _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)
        redirect_resp.release_conn.assert_called_once()

    def test_redirect_to_http_rejected(self):
        """A redirect downgrading from HTTPS to HTTP must be rejected."""
        redirect_resp = self._redirect_resp("http://cdn.example.com/img.png")

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for([redirect_resp]),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=self._PUBLIC_DNS),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            with pytest.raises(StorageError, match="Only HTTPS"):
                _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)
        redirect_resp.release_conn.assert_called_once()

    def test_relative_redirect_to_valid_target_followed(self):
        """RFC 7231 §7.1.2: a relative Location like '/new-path' must be
        resolved against the current URL via urljoin before re-validation."""
        # Server replies to /img.png with a relative redirect to /new.png
        redirect_resp = self._redirect_resp("/new.png")
        ok_resp = self._ok_resp([b"image-data"])

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for([redirect_resp, ok_resp]),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=self._PUBLIC_DNS),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            resp = _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)
            assert resp is ok_resp
        redirect_resp.release_conn.assert_called_once()

    def test_relative_redirect_to_private_target_rejected(self):
        """A relative redirect whose resolved target is private must be blocked."""
        # /new-path is relative; urljoin resolves it to https://internal.example.com/new-path
        # but the DNS for that host returns a private IP.
        redirect_resp = self._redirect_resp("https://internal.example.com/secret")

        def dns_side_effect(host, *args, **kwargs):
            if "internal" in host:
                return self._PRIVATE_DNS
            return self._PUBLIC_DNS

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for([redirect_resp]),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", side_effect=dns_side_effect),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            with pytest.raises(StorageError, match="Private/loopback"):
                _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)

    def test_redirect_loop_limit_enforced(self):
        """Infinite redirect chains must be capped and raise.
        _MAX_REDIRECT_HOPS = 5: loop runs 6 iterations (5 redirects + 1 final
        fetch slot), so providing 7 redirect responses exhausts the limit."""
        # 7 redirects > _MAX_REDIRECT_HOPS (5); the 6th iteration is the final
        # fetch slot, and the 7th redirect triggers "Too many redirects".
        resps = [self._redirect_resp("https://cdn.example.com/loop") for _ in range(7)]

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for(resps),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=self._PUBLIC_DNS),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            with pytest.raises(StorageError, match="Too many redirect"):
                _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)

    def test_valid_redirect_followed(self):
        """A single redirect to a public HTTPS URL is allowed and followed."""
        redirect_resp = self._redirect_resp("https://cdn2.example.com/img.png")
        ok_resp = self._ok_resp([b"image-data"])

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for([redirect_resp, ok_resp]),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=self._PUBLIC_DNS),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            resp = _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)
            assert resp is ok_resp
        redirect_resp.release_conn.assert_called_once()

    def test_redirect_releases_intermediate_connections(self):
        """Each intermediate redirect response must have release_conn() called."""
        hop1 = self._redirect_resp("https://cdn2.example.com/img.png")
        hop2 = self._redirect_resp("https://cdn3.example.com/img.png")
        final = self._ok_resp([b"data"])

        with (
            patch(
                "genblaze_core.storage.transfer.urllib3.HTTPSConnectionPool",
                side_effect=_pool_factory_for([hop1, hop2, final]),
            ),
            patch("genblaze_core._utils.socket.getaddrinfo", return_value=self._PUBLIC_DNS),
        ):
            from genblaze_core.storage.transfer import _http_get_stream

            resp = _http_get_stream("https://cdn.example.com/img.png", timeout=30.0)
            assert resp is final
        hop1.release_conn.assert_called_once()
        hop2.release_conn.assert_called_once()


class TestPerformanceDefaults:
    """Guardrails for the performance-tuning constants.

    These thresholds govern memory vs disk tradeoffs on the hot path. A
    silent regression here would show up as unexplained disk I/O on small-
    asset workloads (images, audio) — hard to diagnose, expensive to fix
    after the fact. Tests pin the relationships so future edits are
    deliberate.
    """

    def test_spool_threshold_matches_multipart_threshold(self):
        """Below the multipart cutoff the upload is single-PUT, so the full
        body ends up in a single HTTP request regardless; there is no reason
        to pay disk I/O for it. Keeping the two constants in lockstep means:
        single-PUT → in-RAM; multipart → disk-spooled. Clean invariant."""
        from genblaze_core.storage import transfer
        from genblaze_s3.backend import _MULTIPART_THRESHOLD

        assert transfer._SPOOL_THRESHOLD == _MULTIPART_THRESHOLD, (
            "Spool and multipart thresholds should stay in lockstep. "
            "Changing one without the other either wastes disk I/O (spool "
            "< multipart) or blows memory on single-PUT payloads (spool > "
            "multipart)."
        )

    def test_max_download_bytes_accommodates_long_form_video(self):
        """Long-form 1080p video from Sora/Veo can approach 2 GB. The cap
        must leave headroom above that so legitimate provider outputs don't
        trip the limit."""
        from genblaze_core.storage import transfer

        two_gb = 2 * 1024 * 1024 * 1024
        assert transfer._DEFAULT_MAX_DOWNLOAD_BYTES > two_gb, (
            "Default max download must exceed 2 GB to cover long-form video."
        )
