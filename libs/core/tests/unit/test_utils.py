"""Tests for shared utility functions in _utils.py."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from unittest.mock import patch

import pytest
from genblaze_core._utils import (
    _is_blocked_ip,
    _normalize_ip,
    _run_async,
    check_ssrf,
    compute_sha256,
    probe_audio_duration,
)
from genblaze_core.exceptions import StorageError
from hypothesis import given
from hypothesis import strategies as st

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

    def test_ipv4_mapped_ipv6_imds_rejected(self):
        """::ffff:169.254.169.254 is the IPv4-mapped IPv6 form of the IMDS address.
        Without normalization it bypasses all IPv4 BLOCKED_NETWORKS entries.
        The normalized form must match the 169.254.0.0/16 block."""
        # AF_INET6 with an IPv4-mapped address in the sockaddr
        mapped_imds = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 0, 0, 0))
        ]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=mapped_imds):
            with pytest.raises(ValueError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/path")

    def test_ipv4_mapped_ipv6_private_rejected(self):
        """::ffff:10.0.0.1 (RFC 1918 via IPv4-mapped IPv6) must be blocked."""
        mapped_private = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:10.0.0.1", 0, 0, 0))
        ]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=mapped_private):
            with pytest.raises(ValueError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/path")

    def test_nat64_imds_rejected(self):
        """64:ff9b::a9fe:a9fe is the RFC 6052 NAT64 form of the IMDS address
        169.254.169.254 (169=0xa9, 254=0xfe). ip.ipv4_mapped only recognizes
        ::ffff:0:0/96, not the NAT64 well-known prefix, so this needs its
        own extraction path."""
        nat64_imds = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::a9fe:a9fe", 0, 0, 0))
        ]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=nat64_imds):
            with pytest.raises(ValueError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/path")

    def test_nat64_private_rejected(self):
        """64:ff9b::0a00:0001 is the NAT64 form of 10.0.0.1."""
        nat64_private = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::0a00:0001", 0, 0, 0))
        ]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=nat64_private):
            with pytest.raises(ValueError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/path")

    def test_nat64_public_allowed(self):
        """NAT64 wrapping a genuinely public IPv4 address must still resolve —
        the extraction path isn't a blanket rejection of the well-known prefix."""
        nat64_public = [
            # 93.184.216.34 = 0x5d.0xb8.0xd8.0x22
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::5db8:d822", 0, 0, 0))
        ]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=nat64_public):
            check_ssrf("https://public.example.com/path")

    def test_unspecified_ipv6_rejected(self):
        """::/128 (the unspecified address) must be blocked."""
        unspecified = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::", 0, 0, 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=unspecified):
            with pytest.raises(ValueError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/path")

    def test_reserved_range_rejected_by_property_backstop(self):
        """192.0.2.0/24 (TEST-NET-1, RFC 5737) isn't in the explicit
        BLOCKED_NETWORKS list but is IETF-reserved; the is_reserved/is_private
        property backstop must still catch ranges the explicit list misses."""
        reserved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))]
        with patch("genblaze_core._utils.socket.getaddrinfo", return_value=reserved):
            with pytest.raises(ValueError, match="Private/loopback"):
                check_ssrf("https://sneaky.example.com/path")


class TestIsBlockedIpProperties:
    """Property-based backstop for #16: no alternate representation of a
    given IPv4 address (IPv4-mapped IPv6, NAT64) should have a different
    blocked/allowed verdict than the plain address — that mismatch is
    exactly the bypass class the issue describes."""

    @given(st.ip_addresses(v=4))
    def test_mapped_and_nat64_forms_match_plain_v4_verdict(self, v4):
        plain_blocked = _is_blocked_ip(v4)

        mapped_v6 = ipaddress.IPv6Address(f"::ffff:{v4}")
        assert _is_blocked_ip(_normalize_ip(mapped_v6)) == plain_blocked

        nat64_base = int(ipaddress.ip_network("64:ff9b::/96").network_address)
        nat64_v6 = ipaddress.IPv6Address(nat64_base | int(v4))
        assert _is_blocked_ip(_normalize_ip(nat64_v6)) == plain_blocked

    @given(st.ip_addresses(v=4, network="10.0.0.0/8"))
    def test_private_v4_range_always_blocked(self, v4):
        assert _is_blocked_ip(_normalize_ip(v4)) is True


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
