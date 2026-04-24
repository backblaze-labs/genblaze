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
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]


# Allowed parent directories for file:// inputs (shared by storage + ffmpeg providers)
# Deduplicate: /tmp and gettempdir() may resolve to the same or different paths
ALLOWED_FILE_ROOTS: tuple[Path, ...] = tuple(
    {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}  # noqa: S108
)


def check_ssrf(url: str, *, exc_type: type[Exception] = ValueError) -> None:
    """Reject non-HTTPS URLs and hostnames resolving to private IP ranges.

    Shared SSRF guard used by storage transfers and webhook dispatch.
    Callers pass their domain-specific exception type via ``exc_type``.
    """
    parsed = _urlparse(url)
    if parsed.scheme not in ("https",):
        raise exc_type(f"Only HTTPS URLs are allowed, got: {parsed.scheme}://")

    host = parsed.hostname or ""
    if host.lower() == "localhost":
        raise exc_type(f"Private/loopback URLs are not allowed: {host}")

    try:
        addrinfos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise exc_type(f"Cannot resolve hostname: {host}") from exc

    for _, _, _, _, sockaddr in addrinfos:
        try:
            ip = ipaddress.ip_address(str(sockaddr[0]))
        except ValueError:
            continue
        if any(ip in net for net in BLOCKED_NETWORKS):
            raise exc_type(f"Private/loopback URLs are not allowed: {host}")


# ---------------------------------------------------------------------------
# Manifest size cap — bounds the JSON payload accepted from disk/media
# ---------------------------------------------------------------------------
# Real manifests are O(KB). 16 MiB is generous and bounds OOM blast from
# malicious media or sidecars that declare absurd payload sizes.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Credential pattern detection — used by error sanitization (providers/base.py)
# AND by Pipeline.step build-time rejection of secret-shaped params values.
# Centralized here so both call sites share one regex of record.
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = re.compile(
    r"(r8_[A-Za-z0-9]{20,})"  # Replicate tokens
    r"|(sk-ant-[A-Za-z0-9\-]{20,})"  # Anthropic API keys (before generic sk-)
    r"|(sk-[A-Za-z0-9]{20,})"  # OpenAI-style keys
    r"|(AIza[A-Za-z0-9_\-]{30,})"  # Google API keys
    r"|(AKIA[A-Z0-9]{16})"  # AWS access key IDs
    r"|(Bearer\s+[A-Za-z0-9._\-]{20,})"  # Bearer tokens
    r"|(Token\s+[A-Za-z0-9._\-]{20,})"  # Token auth headers
    r"|(\bapi[_-]key[=:]\s*[A-Za-z0-9._\-]{20,})",  # api_key=... / api-key:...
    re.IGNORECASE,
)


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
