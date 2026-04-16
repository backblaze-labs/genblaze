"""Shared ffmpeg utilities for compositor and transform providers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from genblaze_core._utils import ALLOWED_FILE_ROOTS as _ALLOWED_FILE_ROOTS
from genblaze_core.exceptions import ProviderError
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
        raw_path = unquote(parsed.path)
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
        from genblaze_core.providers.base import validate_asset_url

        validate_asset_url(url)
        return url
    raise ProviderError(
        f"Unsupported URL scheme '{parsed.scheme}' for ffmpeg input. "
        "Use file:// or https:// URLs.",
        error_code=ProviderErrorCode.INVALID_INPUT,
    )


def run_ffmpeg(
    cmd: list[str],
    timeout: float = 120,
) -> subprocess.CompletedProcess[bytes]:
    """Run an ffmpeg command with timeout and error handling."""
    logger.debug("Running ffmpeg: %s", " ".join(cmd))
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
        stderr = result.stderr.decode(errors="replace")[:500]
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
