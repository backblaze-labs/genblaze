"""Shared ffmpeg utilities for compositor and transform providers."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.request import url2pathname

from genblaze_core._utils import ALLOWED_FILE_ROOTS as _ALLOWED_FILE_ROOTS
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import ProviderErrorCode

# Default subprocess timeout for ffmpeg (seconds)
FFMPEG_TIMEOUT = 120

logger = logging.getLogger("genblaze.ffmpeg")


def resolve_ffmpeg(ffmpeg_path: str = "ffmpeg") -> str:
    """Resolve the ffmpeg binary path; raise if not installed."""
    resolved = shutil.which(ffmpeg_path)
    if resolved is None:
        raise ProviderError(
            f"ffmpeg not found at '{ffmpeg_path}'. "
            "Install ffmpeg: https://ffmpeg.org/download.html",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    return resolved


def resolve_input_path(url: str, *, extra_roots: list[Path] | None = None) -> str:
    """Resolve an asset URL to a path/URL suitable for ffmpeg input.

    Supports file:// (validated to be under temp or extra_roots) and
    https:// (validated and passed directly to ffmpeg).
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        # url2pathname handles Windows drive letters: /C:/... → C:\... (no-op on Unix)
        raw_path = url2pathname(parsed.path)
        resolved = Path(raw_path).resolve()
        allowed = list(_ALLOWED_FILE_ROOTS)
        if extra_roots:
            allowed.extend(r.resolve() for r in extra_roots)
        if not any(resolved.is_relative_to(root) for root in allowed):
            raise ProviderError(
                f"file:// URL outside allowed directories: {resolved}. "
                f"Files must be under temp or output_dir.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        return str(resolved)
    if parsed.scheme == "https":
        from genblaze_core._utils import check_ssrf
        from genblaze_core.providers.base import validate_asset_url

        validate_asset_url(url)
        # SSRF guard: ffmpeg will do its own HTTP fetch; reject private/loopback
        # hosts before we hand off the URL. Without this, a chain input pointing
        # at cloud IMDS (169.254.169.254, metadata.google.internal, etc.) would
        # execute through ffmpeg and exfiltrate credentials.
        # Known gap: ffmpeg follows HTTP redirects internally with no way to
        # intercept Location headers for re-validation. For full redirect safety,
        # pre-download the URL via transfer._http_get_stream before passing to
        # ffmpeg. Tracked as follow-up debt (lower severity: ffmpeg runs in a
        # subprocess, not in-process HTTP client code).
        check_ssrf(url, exc_type=ProviderError)
        return url
    raise ProviderError(
        f"Unsupported URL scheme '{parsed.scheme}' for ffmpeg input. "
        "Use file:// or https:// URLs.",
        error_code=ProviderErrorCode.INVALID_INPUT,
    )


def _redact_url_query(arg: str) -> str:
    """Strip the query string from a URL-shaped command argument.

    A chained step's ``-i <url>`` argument can be a presigned object-storage
    URL (e.g. ``https://...&X-Amz-Signature=...``); the query string is a
    bearer credential for that object until the signature expires. Only
    ``http``/``https`` arguments with a query string are touched — plain
    filter strings, paths, and flags (``-vf``, ``scale=1280:720``, etc.) pass
    through unchanged because ``urlsplit`` reports no scheme for them.
    """
    parsed = urlsplit(arg)
    if parsed.scheme in ("http", "https") and parsed.query:
        return urlunsplit(parsed._replace(query="REDACTED"))
    return arg


def _redact_cmd_for_log(cmd: list[str]) -> str:
    """Render a command list for logging with URL query strings redacted."""
    return " ".join(_redact_url_query(arg) for arg in cmd)


# Matches an http(s) URL with a query string embedded in free-form text (as
# opposed to `_redact_url_query`, which expects the whole argument to be one
# URL). Non-greedy up to the first '?' so a URL followed by other text
# (ffmpeg stderr, not just a bare argument) is captured correctly.
_URL_WITH_QUERY_IN_TEXT_RE = re.compile(r"https?://\S+?\?\S+")


def _redact_urls_in_text(text: str) -> str:
    """Redact the query string of any http(s) URL embedded in free-form text.

    ffmpeg's own stderr can echo a presigned input URL verbatim on a fetch
    failure (e.g. a 403 on an expired signature), and that stderr becomes
    the ``ProviderError`` message — a second leak path for the same
    presigned-URL signature beyond the DEBUG command log (#75).
    """
    return _URL_WITH_QUERY_IN_TEXT_RE.sub(lambda m: _redact_url_query(m.group(0)), text)


def run_ffmpeg(
    cmd: list[str],
    timeout: float = 120,
) -> subprocess.CompletedProcess[bytes]:
    """Run an ffmpeg command with timeout and error handling."""
    # The command actually executed (`cmd`) is untouched; only the DEBUG log
    # line is redacted (#75 — presigned URL query strings must not reach logs).
    logger.debug("Running ffmpeg: %s", _redact_cmd_for_log(cmd))
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            f"ffmpeg timed out after {timeout}s",
            error_code=ProviderErrorCode.TIMEOUT,
        ) from exc
    except OSError as exc:
        raise ProviderError(
            f"Failed to run ffmpeg: {exc}",
            error_code=ProviderErrorCode.UNKNOWN,
        ) from exc

    if result.returncode != 0:
        # Redact before truncating: a signature that straddles the 500-char
        # cutoff would otherwise leak its surviving half.
        stderr = _redact_urls_in_text(result.stderr.decode(errors="replace"))[:500]
        raise ProviderError(
            f"ffmpeg exited with code {result.returncode}: {stderr}",
            error_code=ProviderErrorCode.UNKNOWN,
        )
    return result


def get_output_path(step_id: str, ext: str, output_dir: Path | None) -> Path:
    """Determine output file path for an ffmpeg operation."""
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / f"{step_id}.{ext}"
    fd, tmp = tempfile.mkstemp(suffix=f".{ext}")
    os.close(fd)
    return Path(tmp)


def populate_file_asset_integrity(asset: Asset, path: Path) -> None:
    """Populate ``asset.sha256`` and ``asset.size_bytes`` from a local file."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ProviderError(
            f"Failed to hash ffmpeg output at {path}: {exc}",
            error_code=ProviderErrorCode.UNKNOWN,
        ) from exc
    asset.sha256 = digest.hexdigest()
    asset.size_bytes = size
