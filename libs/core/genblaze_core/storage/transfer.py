"""AssetTransfer — download from CDN, hash, upload to storage backend."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from urllib.parse import urlparse

import urllib3

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

# Files smaller than this stay in memory; larger ones spool to disk. Set to the
# same threshold we use to decide between single-PUT and multipart uploads —
# if a payload would upload in one shot, it fits in RAM too. Eliminates the
# disk round-trip that 1 MB files otherwise paid (images, audio, short clips).
# Peak memory per worker is bounded by this; 4 workers × 16 MB = 64 MB worst
# case, which is fine for anything larger than a tight Lambda.
_SPOOL_THRESHOLD = 16 * 1024 * 1024  # 16 MB — matches the multipart threshold

# Default max download size. Generous enough for long-form generated video
# (multi-minute 1080p from Sora / Veo can approach 2 GB) while still
# protecting against runaway payloads from a misbehaving provider.
_DEFAULT_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB

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


# Shared HTTPS connection pool for asset downloads. urllib.request opens a
# fresh TCP + TLS handshake per call; at 150 ms per handshake a batch run
# with 50 images from the same CDN burns 7.5 s purely on connection setup.
# urllib3's PoolManager reuses connections across the sink's worker threads
# (thread-safe) and across subsequent runs in the same process.
#
# Retry on 429/5xx is built into urllib3, giving us transient-failure
# resilience the previous urllib path lacked entirely.
_HTTP_POOL = urllib3.PoolManager(
    num_pools=10,
    maxsize=20,
    retries=urllib3.Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    ),
)


class _HashingStreamReader:
    """File-like wrapper around an HTTP response for pipelined transfer.

    The spooled transfer path fully downloads to a SpooledTemporaryFile
    before handing bytes to boto3. This wrapper lets boto3's multipart
    machinery read directly from the HTTP response — bytes never touch
    disk. SHA-256 is computed incrementally as boto3 reads chunks, so
    the whole-object hash is available the moment the upload completes.

    Deliberately not inheriting from ``io.RawIOBase`` / ``io.IOBase`` —
    boto3's ``upload_fileobj`` only needs duck-typed ``.read(n)`` and
    (for retry detection) ``.seekable()``. Avoiding the ABC keeps the
    allocation profile simple and the type-checker happy.

    Unseekable by design: boto3 reads multipart chunks sequentially,
    buffering each 16 MB part in memory before upload. Part-level
    retries work against that buffer, not the source stream — so a
    mid-stream retry of the upload leg doesn't require the download to
    rewind (which it can't).
    """

    def __init__(self, resp: Any, *, max_bytes: int) -> None:
        self._resp = resp
        self._hasher = hashlib.sha256()
        self._size = 0
        self._max_bytes = max_bytes

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            # Read-until-EOF per the file-like contract. boto3 passes
            # specific sizes for multipart, but keep the contract honest
            # for callers (tests, future backends) that do .read().
            chunks = []
            while True:
                chunk = self._resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        else:
            data = self._resp.read(size)
        if data:
            self._hasher.update(data)
            self._size += len(data)
            if self._size > self._max_bytes:
                raise StorageError(f"Download exceeds {self._max_bytes} byte limit")
        return data

    def readable(self) -> bool:  # boto3 checks this
        return True

    def seekable(self) -> bool:  # boto3's retry path gates on this
        return False

    @property
    def sha256_hex(self) -> str:
        return self._hasher.hexdigest()

    @property
    def size(self) -> int:
        return self._size


def _http_get_stream(url: str, *, timeout: float) -> Any:
    """Open a streaming GET via the shared urllib3 pool.

    Returns a ``urllib3.HTTPResponse`` in ``preload_content=False`` mode so
    the caller can read chunks via ``resp.read(n)``. The caller MUST call
    ``resp.release_conn()`` in a ``finally`` to return the connection to
    the pool — otherwise the pool exhausts under load.

    Raises ``StorageError`` on HTTP errors and transport failures so
    ``AssetTransfer`` sees a single exception type regardless of the
    underlying failure mode.
    """
    try:
        resp = _HTTP_POOL.request(
            "GET",
            url,
            preload_content=False,
            timeout=urllib3.Timeout(connect=30.0, read=timeout),
            headers={"User-Agent": _USER_AGENT},
        )
    except urllib3.exceptions.HTTPError as exc:
        raise StorageError(f"Download failed for {url}: {exc}") from exc
    if resp.status >= 400:
        resp.release_conn()
        raise StorageError(f"HTTP {resp.status} downloading {url}")
    return resp


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
        allowed_roots: list[Path] | None = None,
        max_download_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES,
        download_timeout: float = _DEFAULT_DOWNLOAD_TIMEOUT,
        pipelined_transfer: bool = False,
    ):
        self._backend = backend
        self._prefix = prefix
        self._strategy = key_strategy
        self._allowed_roots = allowed_roots
        self._max_download_bytes = max_download_bytes
        self._download_timeout = download_timeout
        # When True, stream bytes directly from the HTTP response into the
        # backend's multipart upload — no SpooledTemporaryFile in the middle.
        # Halves wall-clock on large video (download and upload overlap)
        # but costs 2 extra S3 calls per CAS asset (temp-key + copy + delete).
        # Opt-in: users with video-heavy workloads flip this on.
        self._pipelined_transfer = pipelined_transfer

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
            # Remote URL → validate, then either stream to temp file (default)
            # or pipeline directly into the multipart upload (opt-in).
            _validate_url(asset.url)
            if self._pipelined_transfer:
                key, sha256, size = self._transfer_pipelined(
                    asset, tenant=tenant, date_str=date_str, run_id=run_id
                )
            else:
                key, sha256, size = self._transfer_spooled(
                    asset, tenant=tenant, date_str=date_str, run_id=run_id
                )

        # Update asset metadata in place. Use the durable (credential-free)
        # URL — never a presigned URL. The result lands in manifests,
        # parquet sinks, and embedded media; SigV4 signatures must not.
        # Callers needing a fetchable short-lived URL call backend.get_url()
        # directly.
        asset.sha256 = sha256
        asset.size_bytes = size
        asset.url = self._backend.get_durable_url(key)

        return key

    def _transfer_spooled(
        self,
        asset: Asset,
        *,
        tenant: str | None,
        date_str: str | None,
        run_id: str | None,
    ) -> tuple[str, str, int]:
        """Download fully to SpooledTemporaryFile, then upload.

        Default path: simple, handles CAS dedup cheaply (one exists() check
        avoids the temp-key+copy dance the pipelined CAS variant needs).
        Pays a download-then-upload serialization that the pipelined path
        avoids on large files. Returns (key, sha256, size).
        """
        try:
            resp = _http_get_stream(asset.url, timeout=self._download_timeout)
            try:
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
            finally:
                # Return the connection to the pool — otherwise the pool
                # exhausts after maxsize in-flight transfers.
                resp.release_conn()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"Failed to download asset {asset.url}: {exc}") from exc

        return key, sha256, size

    def _transfer_pipelined(
        self,
        asset: Asset,
        *,
        tenant: str | None,
        date_str: str | None,
        run_id: str | None,
    ) -> tuple[str, str, int]:
        """Stream bytes directly from CDN → backend multipart upload.

        Eliminates the download-to-disk intermediate step that the spooled
        path pays on large assets. For a 1 GB video at 100 MB/s both legs:
        spooled wall-clock = 20 s (D + U), pipelined = ~10 s (max(D, U)).

        HIERARCHICAL mode streams straight to the final key (asset_id based,
        known upfront). CAS mode uploads to ``{prefix}/.tmp/{asset_id}.ext``,
        reads the hash from the stream wrapper, then promotes via
        server-side ``copy`` to the content-addressed final key and deletes
        the temp. Two extra transactions for CAS; worth it on video payloads.

        Returns (key, sha256, size).
        """
        resp = _http_get_stream(asset.url, timeout=self._download_timeout)
        temp_key: str | None = None
        try:
            content_type = resp.headers.get("Content-Type")
            ext = _guess_extension(asset.url, content_type)
            reader = _HashingStreamReader(resp, max_bytes=self._max_download_bytes)
            cache_control = {"CacheControl": _cache_control_for(self._strategy)}

            if self._strategy == KeyStrategy.HIERARCHICAL:
                # Key known upfront — stream directly.
                key = _build_key(
                    self._strategy,
                    self._prefix,
                    asset,
                    "",  # unused for HIERARCHICAL
                    ext,
                    tenant=tenant,
                    date_str=date_str,
                    run_id=run_id,
                )
                self._backend.put(
                    key,
                    cast(BinaryIO, reader),
                    content_type=content_type,
                    extra_args=cache_control,
                )
            else:
                # CAS: upload to temp key, then promote based on hash.
                temp_key = f"{self._prefix}/.tmp/{asset.asset_id}{ext}"
                self._backend.put(
                    temp_key,
                    cast(BinaryIO, reader),
                    content_type=content_type,
                    extra_args=cache_control,
                )
                final_key = _build_key(
                    self._strategy,
                    self._prefix,
                    asset,
                    reader.sha256_hex,
                    ext,
                    tenant=tenant,
                    date_str=date_str,
                    run_id=run_id,
                )
                if self._backend.exists(final_key):
                    # Dedup hit — discard our copy.
                    self._backend.delete(temp_key)
                    temp_key = None
                else:
                    # Promote: server-side copy, then delete temp.
                    self._backend.copy(temp_key, final_key)
                    self._backend.delete(temp_key)
                    temp_key = None
                key = final_key

            sha256 = reader.sha256_hex
            size = reader.size
        except StorageError:
            # Defensive: if we uploaded to temp_key before the error, try
            # to clean it up so orphans don't accrue.
            if temp_key is not None:
                try:
                    self._backend.delete(temp_key)
                except Exception:
                    logger.warning("Failed to clean up temp key %s after error", temp_key)
            raise
        except Exception as exc:
            if temp_key is not None:
                try:
                    self._backend.delete(temp_key)
                except Exception:
                    logger.warning("Failed to clean up temp key %s after error", temp_key)
            raise StorageError(f"Failed to download asset {asset.url}: {exc}") from exc
        finally:
            resp.release_conn()

        return key, sha256, size

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
