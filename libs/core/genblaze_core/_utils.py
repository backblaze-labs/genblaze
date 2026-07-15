"""Internal utilities."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import random
import re
import socket
import tempfile
import uuid
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse as _urlparse


def new_id() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(UTC)


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def normalize_tenant_id(tenant_id: str | None) -> str | None:
    """Normalize a tenant identifier: strip surrounding whitespace, "" -> None.

    Tenancy must read identically wherever it is used (cache key, Run metadata,
    sinks), so empty / whitespace-only values collapse to ``None`` in one place
    rather than being special-cased at each call site.
    """
    if tenant_id is None:
        return None
    return tenant_id.strip() or None


def _run_async(coro: Coroutine) -> Any:
    """Run an async coroutine from sync code safely.

    If there's already a running event loop (e.g. inside Jupyter or an async provider
    called from BaseProvider.invoke), runs in a new thread to avoid RuntimeError.
    Otherwise uses asyncio.run().
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Already inside an event loop — run in a separate thread
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# SSRF protection — shared by storage/transfer.py and webhooks/notifier.py
# ---------------------------------------------------------------------------

# Private/reserved IP ranges blocked to prevent SSRF
BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),  # "This host" — resolves to loopback on Linux
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / IMDS
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("::/128"),  # IPv6 unspecified address
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]

# RFC 6052 "Well-Known Prefix" for NAT64 — the embedded IPv4 address occupies
# the low 32 bits. Distinct from IPv4-mapped IPv6 (::ffff:0:0/96, unwrapped
# via ip.ipv4_mapped below): a NAT64-translating resolver can hand back this
# form for a name that maps to a private/IMDS IPv4 target, and neither
# BLOCKED_NETWORKS nor ``ipv4_mapped`` recognizes it without explicit
# extraction.
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _normalize_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv4-mapped IPv6 and NAT64 well-known-prefix addresses to their
    embedded IPv4 form. Returns ``ip`` unchanged for anything else (plain
    IPv4, or IPv6 that isn't one of these two embedding schemes) so the
    blocklist/backstop check below always sees the "real" target address.
    """
    if ip.version != 6:
        return ip
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return mapped
    if ip in _NAT64_WELL_KNOWN_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` (already passed through :func:`_normalize_ip`) is a
    private/loopback/link-local/reserved/unspecified address.

    Combines the explicit ``BLOCKED_NETWORKS`` denylist (covers the specific
    cloud-metadata address and ranges the stdlib properties below don't
    flag) with a property-based backstop, so a gap in either approach is
    covered by the other rather than compounding.
    """
    return (
        any(ip in net for net in BLOCKED_NETWORKS)
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


# Allowed parent directories for file:// inputs (shared by storage + ffmpeg providers)
# Deduplicate: /tmp and gettempdir() may resolve to the same or different paths
ALLOWED_FILE_ROOTS: tuple[Path, ...] = tuple(
    {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}  # noqa: S108
)


def resolve_ssrf(url: str, *, exc_type: type[Exception] = ValueError) -> tuple[str, str, int]:
    """Validate a URL against SSRF rules and return the pinned IP for the connection.

    Resolves DNS once, validates every returned address against BLOCKED_NETWORKS,
    and returns the first safe IP string. Callers MUST connect to this IP (rather
    than re-resolving the hostname) so that the validated address is the one
    actually reached — eliminating the DNS rebinding / TOCTOU window where the
    HTTP client would independently re-resolve and potentially reach a different,
    private address.

    Returns:
        (pinned_ip, hostname, port) — use ``pinned_ip`` as the connection target,
        ``hostname`` as the TLS SNI / Host header, ``port`` as the TCP port
        (443 when not specified in the URL).

    Raises:
        exc_type: on scheme violation, private/loopback IP, or DNS failure.
    """
    parsed = _urlparse(url)
    if parsed.scheme not in ("https",):
        raise exc_type(f"Only HTTPS URLs are allowed, got: {parsed.scheme}://")

    host = parsed.hostname or ""
    if host.lower() == "localhost":
        raise exc_type(f"Private/loopback URLs are not allowed: {host}")

    port = parsed.port or 443

    try:
        addrinfos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise exc_type(f"Cannot resolve hostname: {host}") from exc

    pinned_ip: str | None = None
    for _, _, _, _, sockaddr in addrinfos:
        raw_ip = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        # Normalize IPv4-mapped IPv6 (::ffff:169.254.x.x, ::ffff:10.x.x.x, etc.)
        # and NAT64 (64:ff9b::/96) so the IPv4 BLOCKED_NETWORKS entries match.
        # Without this, an attacker who controls DNS can return one of these
        # forms and bypass the IPv4 blocklist while the OS connects to the
        # embedded private/IMDS IPv4 target.
        if _is_blocked_ip(_normalize_ip(ip)):
            raise exc_type(f"Private/loopback URLs are not allowed: {host}")
        if pinned_ip is None:
            pinned_ip = raw_ip  # pin to the first validated address

    if pinned_ip is None:
        raise exc_type(f"Cannot resolve hostname: {host}")

    return pinned_ip, host, port


def open_pinned_https_connection(
    url: str,
    *,
    timeout: float,
    exc_type: type[Exception] = ValueError,
) -> Any:
    """Validate ``url``, resolve DNS once, and return a TLS-connected HTTPSConnection.

    The TCP socket is opened to the pinned IP returned by ``resolve_ssrf``
    rather than letting ``http.client`` re-resolve the hostname. This closes
    the DNS rebinding / TOCTOU window. TLS SNI and certificate verification
    still use the original hostname, so the TLS handshake is meaningful.

    The caller MUST close the returned connection (``conn.close()``) in a
    ``finally`` block — not just on the success path. The connection is live;
    leaking it leaves the underlying OS socket open until GC collects it.

    Note: outbound connections bypass HTTP(S)_PROXY / NO_PROXY env vars by
    design — IP pinning requires a direct TCP connection to the validated
    address; routing through a proxy would re-introduce the TOCTOU window.

    Raises:
        exc_type: on scheme violation, private/loopback IP, DNS failure, or
            any socket/TLS error encountered while establishing the connection.
    """
    import http.client
    import socket as _socket
    import ssl

    pinned_ip, host, port = resolve_ssrf(url, exc_type=exc_type)
    ctx = ssl.create_default_context()
    # Explicitly require TLS 1.2+ — create_default_context() already does this
    # in CPython 3.10+, but setting it explicitly silences static analyzers and
    # makes the minimum-version contract clear to future readers.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    raw_sock: _socket.socket | None = None
    try:
        raw_sock = _socket.create_connection((pinned_ip, port), timeout=timeout)
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        conn = http.client.HTTPSConnection(host, port, context=ctx)
        conn.sock = tls_sock  # inject pre-connected + TLS-wrapped socket
    except Exception:
        if raw_sock is not None:
            raw_sock.close()
        raise
    return conn


def check_ssrf(url: str, *, exc_type: type[Exception] = ValueError) -> None:
    """Reject non-HTTPS URLs and hostnames resolving to private IP ranges.

    Shared SSRF guard used where the caller does not need the resolved IP.
    For paths that establish HTTP connections, prefer ``resolve_ssrf`` which
    returns the pinned IP — connecting to it eliminates the DNS rebinding
    TOCTOU window.
    """
    resolve_ssrf(url, exc_type=exc_type)  # validate; discard pinned IP


# ---------------------------------------------------------------------------
# Manifest size cap — bounds the JSON payload accepted from disk/media
# ---------------------------------------------------------------------------
# Real manifests are O(KB). 16 MiB is generous and bounds OOM blast from
# malicious media or sidecars that declare absurd payload sizes.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Credential pattern detection — used by shared error sanitization
# AND by Pipeline.step build-time rejection of secret-shaped params values.
# Centralized here so both call sites share one regex of record.
# ---------------------------------------------------------------------------
MAX_ERROR_LENGTH = 500
TRUNCATION_MARKER = "...(truncated)"
_SANITIZE_SCAN_LIMIT = MAX_ERROR_LENGTH * 4
_AWS_CREDENTIAL_VALUE_RE = (
    r"(?<![A-Za-z0-9/+])"
    r"(?=[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+]))"
    r"(?=[A-Za-z0-9/+]*[+/])"
    r"(?=[A-Za-z0-9/+]*[A-Z])"
    r"(?=[A-Za-z0-9/+]*[a-z])"
    r"[A-Za-z0-9/+]{40}"
)

_SECRET_PATTERNS = re.compile(
    r"(r8_[A-Za-z0-9]{20,})"  # Replicate tokens
    r"|(sk-ant-[A-Za-z0-9\-]{20,})"  # Anthropic API keys (before generic sk-)
    r"|(sk-[A-Za-z0-9]{20,})"  # OpenAI-style keys
    r"|(AIza[A-Za-z0-9_\-]{30,})"  # Google API keys
    r"|(AKIA[A-Z0-9]{16})"  # AWS access key IDs
    rf"|({_AWS_CREDENTIAL_VALUE_RE})"  # AWS secret access keys
    r"|(\bK005[A-Za-z0-9+/]{20,})"  # Backblaze B2 application keys
    r"|(https?://[^/\s:@]+:[^/\s@]{12,}@)"  # Basic-auth URL credentials
    r"|(\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)"  # JWTs
    r"|(Bearer\s+[A-Za-z0-9._\-]{20,})"  # Bearer tokens
    r"|(Token\s+[A-Za-z0-9._\-]{20,})"  # Token auth headers
    r"|(\bapi[_-]key[=:]\s*[A-Za-z0-9._\-]{20,})",  # api_key=... / api-key:...
    re.IGNORECASE,
)


def sanitize_error(msg: str) -> str:
    """Redact potential secrets and truncate error messages for safe storage."""
    bounded = msg[:_SANITIZE_SCAN_LIMIT]
    input_truncated = len(msg) > _SANITIZE_SCAN_LIMIT
    sanitized = _SECRET_PATTERNS.sub("[REDACTED]", bounded)
    if input_truncated or len(sanitized) > MAX_ERROR_LENGTH:
        sanitized = sanitized[:MAX_ERROR_LENGTH] + TRUNCATION_MARKER
    return sanitized


def jittered_backoff(attempt: int) -> float:
    """Exponential backoff with AWS-style full jitter — decorrelates parallel clients.

    Returns a value in [0, min(2**attempt, 30)). Full jitter (vs. additive jitter)
    is what actually de-syncs a thundering herd: 50 clients hitting a shared
    hiccup land uniformly across the window instead of bunching near the top.
    """
    cap = min(2**attempt, 30)
    return random.uniform(0, cap)  # noqa: S311 — jitter, not crypto


def probe_audio_duration(path: str | Any) -> float | None:
    """Try to read audio duration from a file using mutagen (optional dep).

    Returns duration in seconds, or None if mutagen is not installed or
    the file format is not recognized.
    """
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path))
        if audio is not None and audio.info is not None:
            return audio.info.length
    except Exception:  # noqa: S110 — mutagen is optional, fail gracefully
        pass
    return None
