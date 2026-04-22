"""AssetTransfer — download from CDN, hash, upload to storage backend."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, cast
from urllib.parse import urlparse

from genblaze_core._utils import ALLOWED_FILE_ROOTS, check_ssrf
from genblaze_core._version import __version__
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.base import KeyStrategy

if TYPE_CHECKING:
    from genblaze_core.models.asset import Asset
    from genblaze_core.storage.base import StorageBackend

logger = logging.getLogger("genblaze.storage.transfer")

# 256KB read chunks — balances memory and throughput for large video files
_CHUNK_SIZE = 256 * 1024

# Files smaller than this stay in memory; larger ones spool to disk
_SPOOL_THRESHOLD = 1_048_576  # 1MB

# Default max download size (2 GB) — prevents resource exhaustion from oversized responses
_DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024

# Default HTTP timeout for remote downloads (seconds)
_DEFAULT_DOWNLOAD_TIMEOUT = 60

# Only allow HTTPS downloads for remote URLs; file:// handled separately
_ALLOWED_SCHEMES = frozenset({"https"})

# Schemes allowed for local-file-to-cloud upload
_LOCAL_SCHEMES = frozenset({"file"})

# User agent for HTTP requests (asset downloads from B2/CDN)
_USER_AGENT = f"b2ai-genblaze/{__version__}"

# Cache-Control values. CAS keys are SHA-256-derived so content at that key
# is immutable by construction — mark for long CDN caching. HIERARCHICAL
# keys are UUID-based but per-run, so a shorter TTL is safer.
_CAS_CACHE_CONTROL = "public, max-age=31536000, immutable"
_HIERARCHICAL_CACHE_CONTROL = "private, max-age=3600"


def _cache_control_for(strategy: KeyStrategy) -> str:
    """Pick the right Cache-Control value for an asset key strategy."""
    if strategy == KeyStrategy.CONTENT_ADDRESSABLE:
        return _CAS_CACHE_CONTROL
    return _HIERARCHICAL_CACHE_CONTROL


def _validate_url(url: str) -> None:
    """Reject non-HTTPS URLs and private/reserved IP ranges (SSRF protection)."""
    check_ssrf(url, exc_type=StorageError)


def _guess_extension(url: str, content_type: str | None) -> str:
    """Guess file extension from URL path or content type."""
    path_ext = PurePosixPath(urlparse(url).path).suffix
    if path_ext:
        return path_ext
    if content_type:
        ext = mimetypes.guess_extension(content_type)
        if ext:
            return ext
    return ""


def _build_key(
    strategy: KeyStrategy,
    prefix: str,
    asset: Asset,
    sha256: str,
    ext: str,
    *,
    tenant: str | None = None,
    date_str: str | None = None,
    run_id: str | None = None,
) -> str:
    """Build storage key based on strategy."""
    if strategy == KeyStrategy.CONTENT_ADDRESSABLE:
        return f"{prefix}/{sha256[:2]}/{sha256[2:4]}/{sha256}{ext}"
    # HIERARCHICAL — group assets under run folder
    parts = [prefix]
    if tenant:
        parts.append(tenant)
    if date_str:
        parts.append(date_str)
    if run_id:
        parts.append(run_id)
    parts.append("assets")
    parts.append(f"{asset.asset_id}{ext}")
    return "/".join(parts)


def _read_local_file(
    url: str, *, extra_roots: list[Path] | None = None
) -> tuple[bytes, str | None]:
    """Read a file:// URL and return (bytes, content_type).

    Uses an allowlist of permitted directories (temp dirs + caller-specified roots)
    to prevent arbitrary file reads. Resolves symlinks before checking.
    """
    parsed = urlparse(url)
    from urllib.parse import unquote

    path = unquote(parsed.path)
    resolved = Path(path).resolve()

    # Allowlist: only temp dirs and explicitly provided roots
    allowed = list(ALLOWED_FILE_ROOTS)
    if extra_roots:
        allowed.extend(r.resolve() for r in extra_roots)

    if not any(resolved.is_relative_to(root) for root in allowed):
        raise StorageError(
            f"Access denied: local file path {resolved} is outside allowed directories. "
            f"Files must be under temp or output_dir."
        )

    try:
        data = resolved.read_bytes()
    except Exception as exc:
        raise StorageError(f"Failed to read local file {path}: {exc}") from exc
    content_type, _ = mimetypes.guess_type(str(resolved))
    return data, content_type


class AssetTransfer:
    """Download assets from CDN URLs, hash them, and upload to storage.

    Streams through SHA-256 hash to avoid loading entire files into memory.
    Also supports file:// URIs for local-file-to-cloud upload.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        prefix: str = "assets",
        key_strategy: KeyStrategy = KeyStrategy.CONTENT_ADDRESSABLE,
        url_expires_in: int = 3600,
        allowed_roots: list[Path] | None = None,
        max_download_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES,
        download_timeout: float = _DEFAULT_DOWNLOAD_TIMEOUT,
    ):
        self._backend = backend
        self._prefix = prefix
        self._strategy = key_strategy
        self._url_expires_in = url_expires_in
        self._allowed_roots = allowed_roots
        self._max_download_bytes = max_download_bytes
        self._download_timeout = download_timeout

    def transfer(
        self,
        asset: Asset,
        *,
        tenant: str | None = None,
        date_str: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Download from asset.url, hash, upload, update asset in place. Returns storage key."""
        parsed = urlparse(asset.url)

        if parsed.scheme in _LOCAL_SCHEMES:
            # Local file → read directly (allowlisted dirs only)
            data, content_type = _read_local_file(asset.url, extra_roots=self._allowed_roots)
            sha256 = hashlib.sha256(data).hexdigest()
            size = len(data)
            ext = _guess_extension(asset.url, content_type)

            key = _build_key(
                self._strategy,
                self._prefix,
                asset,
                sha256,
                ext,
                tenant=tenant,
                date_str=date_str,
                run_id=run_id,
            )
            if self._strategy == KeyStrategy.CONTENT_ADDRESSABLE and self._backend.exists(key):
                logger.debug("Asset already exists at %s, skipping upload", key)
            else:
                self._backend.put(
                    key,
                    data,
                    content_type=content_type,
                    extra_args={"CacheControl": _cache_control_for(self._strategy)},
                )
        else:
            # Remote URL → validate and stream to temp file (avoids holding large videos in RAM)
            _validate_url(asset.url)
            try:
                req = urllib.request.Request(asset.url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
                with urllib.request.urlopen(req, timeout=self._download_timeout) as resp:  # noqa: S310
                    content_type = resp.headers.get("Content-Type")
                    hasher = hashlib.sha256()
                    size = 0
                    tmp = tempfile.SpooledTemporaryFile(max_size=_SPOOL_THRESHOLD)
                    try:
                        while True:
                            chunk = resp.read(_CHUNK_SIZE)
                            if not chunk:
                                break
                            hasher.update(chunk)
                            tmp.write(chunk)
                            size += len(chunk)
                            if size > self._max_download_bytes:
                                raise StorageError(
                                    f"Download exceeds {self._max_download_bytes} byte limit"
                                )

                        sha256 = hasher.hexdigest()
                        ext = _guess_extension(asset.url, content_type)

                        key = _build_key(
                            self._strategy,
                            self._prefix,
                            asset,
                            sha256,
                            ext,
                            tenant=tenant,
                            date_str=date_str,
                            run_id=run_id,
                        )

                        # Skip upload if content-addressable and already exists
                        already_exists = (
                            self._strategy == KeyStrategy.CONTENT_ADDRESSABLE
                            and self._backend.exists(key)
                        )
                        if already_exists:
                            logger.debug("Asset already exists at %s, skipping upload", key)
                        else:
                            tmp.seek(0)
                            self._backend.put(
                                key,
                                cast(BinaryIO, tmp),
                                content_type=content_type,
                                extra_args={"CacheControl": _cache_control_for(self._strategy)},
                            )
                    finally:
                        tmp.close()
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError(f"Failed to download asset {asset.url}: {exc}") from exc

        # Update asset metadata in place
        asset.sha256 = sha256
        asset.size_bytes = size
        asset.url = self._backend.get_url(key, expires_in=self._url_expires_in)

        return key

    async def atransfer(
        self,
        asset: Asset,
        *,
        tenant: str | None = None,
        date_str: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Async variant — runs transfer in a thread."""
        return await asyncio.to_thread(
            self.transfer, asset, tenant=tenant, date_str=date_str, run_id=run_id
        )
