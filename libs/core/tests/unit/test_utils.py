"""Tests for shared utility functions in _utils.py."""

from __future__ import annotations

import asyncio
import hashlib
import socket
from unittest.mock import patch

import pytest
from genblaze_core._utils import (
    _run_async,
    check_ssrf,
    compute_sha256,
    probe_audio_duration,
)
from genblaze_core.exceptions import StorageError

# Fake DNS results for test control
_PUBLIC_V4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
_LOOPBACK_V4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
_PRIVATE_V4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
_ZERO_NET_V4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.1", 0))]
_LOOPBACK_V6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]
_ULA_V6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fc00::1", 0, 0, 0))]
_LINK_LOCAL_V6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 0, 0, 0))]
_PUBLIC_V6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2607:f8b0:4004:800::200e", 0, 0, 0))]


class TestCheckSsrf:
    """Direct tests for the shared check_ssrf() function."""

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_PUBLIC_V4)
    def test_https_public_ip_allowed(self, _):
        check_ssrf("https://example.com/file.png")

    def test_http_rejected(self):
        with pytest.raises(ValueError, match="Only HTTPS"):
            check_ssrf("http://example.com/file.png")

    def test_ftp_rejected(self):
        with pytest.raises(ValueError, match="Only HTTPS"):
            check_ssrf("ftp://example.com/file.png")

    def test_localhost_rejected(self):
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://localhost/path")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_LOOPBACK_V4)
    def test_loopback_ip_rejected(self, _):
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://sneaky.example.com/path")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_PRIVATE_V4)
    def test_private_ip_rejected(self, _):
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://internal.example.com/path")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_LOOPBACK_V6)
    def test_ipv6_loopback_rejected(self, _):
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://v6host.example.com/path")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_ULA_V6)
    def test_ipv6_unique_local_rejected(self, _):
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://v6ula.example.com/path")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_LINK_LOCAL_V6)
    def test_ipv6_link_local_rejected(self, _):
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://v6link.example.com/path")

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_PUBLIC_V6)
    def test_ipv6_public_allowed(self, _):
        check_ssrf("https://v6public.example.com/path")

    def test_custom_exc_type(self):
        """exc_type parameter controls the raised exception class."""
        with pytest.raises(StorageError, match="Only HTTPS"):
            check_ssrf("http://example.com/file", exc_type=StorageError)

    @patch("genblaze_core._utils.socket.getaddrinfo", return_value=_ZERO_NET_V4)
    def test_zero_network_rejected(self, _):
        """0.0.0.0/8 resolves to loopback on Linux — must be blocked."""
        with pytest.raises(ValueError, match="Private/loopback"):
            check_ssrf("https://sneaky.example.com/path")

    @patch(
        "genblaze_core._utils.socket.getaddrinfo",
        side_effect=socket.gaierror("Name not found"),
    )
    def test_unresolvable_host_rejected(self, _):
        with pytest.raises(ValueError, match="Cannot resolve"):
            check_ssrf("https://nxdomain.invalid/path")


class TestComputeSha256:
    def test_known_digest(self):
        expected = hashlib.sha256(b"hello").hexdigest()
        assert compute_sha256(b"hello") == expected

    def test_empty_bytes(self):
        expected = hashlib.sha256(b"").hexdigest()
        assert compute_sha256(b"") == expected


class TestRunAsync:
    def test_runs_coroutine_from_sync(self):
        async def add(a, b):
            return a + b

        assert _run_async(add(1, 2)) == 3

    def test_runs_coroutine_inside_event_loop(self):
        """When already inside an event loop, _run_async uses a thread."""

        async def outer():
            async def inner():
                return 42

            return _run_async(inner())

        result = asyncio.run(outer())
        assert result == 42


class TestProbeAudioDuration:
    def test_returns_none_for_nonexistent_file(self):
        assert probe_audio_duration("/nonexistent/file.mp3") is None

    def test_returns_none_for_non_audio_file(self, tmp_path):
        f = tmp_path / "not_audio.txt"
        f.write_text("hello")
        assert probe_audio_duration(str(f)) is None
