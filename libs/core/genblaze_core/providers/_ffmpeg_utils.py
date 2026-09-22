"""Shared ffmpeg utilities for deterministic (local ffmpeg) providers.

Used by the built-in ``FFmpegCompositor`` / ``FFmpegTransform`` and public API
for third-party providers via ``genblaze_core.providers`` (#195). The stable
subset is ``FFMPEG_TIMEOUT``, ``resolve_ffmpeg``, ``resolve_input_path``,
``run_ffmpeg``, ``get_output_path`` and ``populate_file_asset_integrity``;
underscored names here are implementation details.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
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
    """Resolve the ffmpeg binary to an absolute path via ``shutil.which``.

    Args:
        ffmpeg_path: Binary name looked up on ``PATH``, or an explicit path.

    Returns:
        The resolved executable path — use it as ``cmd[0]`` for ``run_ffmpeg``.

    Raises:
        ProviderError: ``INVALID_INPUT`` when ffmpeg is not installed/executable.
    """
    resolved = shutil.which(ffmpeg_path)
    if resolved is None:
        raise ProviderError(
            f"ffmpeg not found at '{ffmpeg_path}'. "
            "Install ffmpeg: https://ffmpeg.org/download.html",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    return resolved


def resolve_input_path(url: str, *, extra_roots: Sequence[str | Path] | None = None) -> str:
    """Resolve and validate an asset URL for use as an ffmpeg ``-i`` argument.

    * ``file://`` — resolved (symlinks and ``..`` collapsed) and accepted only
      under the system temp dirs or one of ``extra_roots``.
    * ``https://`` — URL-validated and SSRF-checked (private/loopback/IMDS hosts
      rejected), then returned unchanged for ffmpeg to fetch. ffmpeg follows
      redirects and re-resolves DNS itself, which this check cannot re-validate;
      pre-download untrusted URLs if that matters to you.

    The result is always an absolute path or an ``https://`` URL, so it can never
    be misread by ffmpeg as a ``-flag``. Only the top-level URL is checked: a
    playlist input (HLS ``#EXTM3U``) can make ffmpeg open further URLs/files, so
    put ``-protocol_whitelist`` (and ``-f <format>`` when known) before ``-i``
    for untrusted inputs.

    Args:
        url: The input asset URL (typically ``step.inputs[i].url``).
        extra_roots: Additional directories ``file://`` inputs may live under,
            e.g. the provider's ``output_dir``.

    Raises:
        ProviderError: ``INVALID_INPUT`` for disallowed paths, unsupported
            schemes, or URLs failing validation/SSRF checks.
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        # url2pathname handles Windows drive letters: /C:/... → C:\... (no-op on Unix)
        raw_path = url2pathname(parsed.path)
        resolved = Path(raw_path).resolve()
        allowed = list(_ALLOWED_FILE_ROOTS)
        if extra_roots:
            allowed.extend(Path(r).resolve() for r in extra_roots)
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
    timeout: float = FFMPEG_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """Run an ffmpeg command as an argument list (never through a shell).

    Presigned-URL query strings are redacted from the DEBUG command log and from
    the stderr excerpt in raised errors; the executed command is unchanged.

    Args:
        cmd: Full argument vector, ``cmd[0]`` being the binary from
            ``resolve_ffmpeg``. Pass each argument as its own element.
        timeout: Seconds before the process is killed.

    Returns:
        The completed process. stdout/stderr are buffered in memory, so write
        output to a file path (``get_output_path``), never ``pipe:1``.

    Raises:
        ProviderError: ``TIMEOUT`` on timeout; ``UNKNOWN`` when the process
            cannot start or exits non-zero (message holds up to 500 chars of
            redacted stderr).
    """
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


# Extensions are interpolated into both the output_dir filename and the mkstemp
# suffix; alphanumerics only keeps "../x" or "a/b" from escaping the directory.
_SAFE_EXT_RE = re.compile(r"[A-Za-z0-9]+")


def get_output_path(step_id: str, ext: str, output_dir: str | Path | None) -> Path:
    """Return the output file path for an ffmpeg operation.

    With ``output_dir``, returns the absolute ``output_dir / f"{step_id}.{ext}"``
    (creating the directory; an existing file is overwritten by ffmpeg ``-y``, so
    don't use a shared world-writable directory). Without it, creates an empty
    temp file via ``mkstemp`` and returns its path.

    Args:
        step_id: Filename stem, normally ``step.step_id`` (a UUID). Must be a
            single path component — no ``/``, ``\\``, ``:`` or NUL. Unused for
            temp files.
        ext: Extension without the dot (e.g. ``"mp4"``); alphanumeric only.
        output_dir: Destination directory, or ``None`` for the system temp dir.

    Raises:
        ProviderError: ``INVALID_INPUT`` when ``step_id`` or ``ext`` could
            produce a path outside the destination directory.
    """
    if not _SAFE_EXT_RE.fullmatch(ext):
        raise ProviderError(
            f"Invalid output ext {ext!r}: use alphanumerics only, without a leading dot.",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    if output_dir:
        # Block separators rather than whitelisting: a user-set step_id like
        # "intro clip" is a legitimate filename and worked before #195. ':' is a
        # Windows drive ("D:evil" escapes the dir) / NTFS alternate data stream.
        if not step_id or any(c in step_id for c in ("/", "\\", ":", "\x00")):
            raise ProviderError(
                f"Invalid step_id {step_id!r} for an output filename: "
                "it must be non-empty and contain no '/', '\\', ':' or NUL.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        # Absolute so a relative dir can't yield "-x.mp4" / "pipe:..." that ffmpeg
        # would parse as a flag or protocol; absolute() keeps symlinks as given.
        out_dir = Path(output_dir).absolute()
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{step_id}.{ext}"
    fd, tmp = tempfile.mkstemp(suffix=f".{ext}")
    os.close(fd)
    return Path(tmp)


def populate_file_asset_integrity(asset: Asset, path: Path) -> None:
    """Stream ``path`` to set ``asset.sha256`` (hex) and ``asset.size_bytes``.

    Reads in 1 MiB chunks, so memory stays flat for large outputs.

    Raises:
        ProviderError: ``UNKNOWN`` when the file cannot be read.
    """
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
