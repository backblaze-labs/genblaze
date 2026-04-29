"""StorageConfig — tunable knobs for object-storage backends.

Frozen dataclass mirroring the ``RetryPolicy`` precedent in
:mod:`genblaze_core.providers.retry`: behavior config (not a wire model),
hashable, immutable, slot-allocated. Defaults preserve the current
``S3StorageBackend`` behavior so passing ``StorageConfig()`` is a no-op
upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Defaults below mirror the current hardcoded values in
# ``libs/connectors/s3/genblaze_s3/backend.py`` (lines 27-33). Holding them
# here as the single source of truth lets callers override per-backend
# without subclassing, and keeps the s3 connector free of magic numbers.
_DEFAULT_MAX_POOL_CONNECTIONS = 20
_DEFAULT_CONNECT_TIMEOUT_SEC = 30.0
_DEFAULT_READ_TIMEOUT_SEC = 300.0
_DEFAULT_MULTIPART_THRESHOLD = 16 * 1024 * 1024
_DEFAULT_MULTIPART_CHUNK_SIZE = 16 * 1024 * 1024
_DEFAULT_RETRIES = 3


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Tunable knobs for an object-storage backend.

    Every field is optional with a default chosen to preserve current behavior.
    Pass ``StorageConfig(...)`` to a backend constructor to override one or
    more knobs without subclassing.

    Attributes:
        max_pool_connections: urllib3 pool size for the underlying client.
            Should be ≥ ``max_concurrency × multipart_concurrency`` for the
            transfer config; default 20 covers 4 asset workers × 4 part
            workers with headroom for HEAD/auth prefetch.
        connect_timeout_sec: TCP connect timeout per request.
        read_timeout_sec: Read timeout per request. Boto3's 60s default
            fires mid-upload on slow links with multi-GB payloads — 300s
            matches the historical S3 backend value.
        multipart_threshold: Single-PUT vs multipart cutoff in bytes.
            Above this, transfers split into ``multipart_chunk_size`` parts
            uploaded in parallel and individually retryable.
        multipart_chunk_size: Per-part size for multipart uploads, in bytes.
        retries: Maximum boto3-internal retry attempts (adaptive mode). Stack
            with the application-level retry loop in :class:`RetryPolicy`.
        user_agent_extra: Optional string appended to the backend's user-agent
            header. Used by Backblaze sample-app convention to attribute the
            calling app (``"<app-slug>/<version>"``); composes on top of the
            backend's hardcoded ``b2ai-genblaze/<version>`` prefix.
        signing_addressing_style: SigV4 addressing style: ``"virtual"`` (the
            current default, ``bucket.host``) or ``"path"`` (``host/bucket``).
            Some on-prem S3-compat endpoints require path-style.
    """

    max_pool_connections: int = _DEFAULT_MAX_POOL_CONNECTIONS
    connect_timeout_sec: float = _DEFAULT_CONNECT_TIMEOUT_SEC
    read_timeout_sec: float = _DEFAULT_READ_TIMEOUT_SEC
    multipart_threshold: int = _DEFAULT_MULTIPART_THRESHOLD
    multipart_chunk_size: int = _DEFAULT_MULTIPART_CHUNK_SIZE
    retries: int = _DEFAULT_RETRIES
    user_agent_extra: str | None = None
    signing_addressing_style: Literal["virtual", "path"] = "virtual"

    def __post_init__(self) -> None:
        # Frozen dataclass blocks attribute assignment after init; we still
        # validate here so an accidentally-zero pool size or negative timeout
        # surfaces at construction time, not during the first slow upload.
        if self.max_pool_connections < 1:
            raise ValueError(
                f"StorageConfig.max_pool_connections must be ≥ 1, got {self.max_pool_connections}"
            )
        if self.connect_timeout_sec <= 0:
            raise ValueError(
                f"StorageConfig.connect_timeout_sec must be > 0, got {self.connect_timeout_sec}"
            )
        if self.read_timeout_sec <= 0:
            raise ValueError(
                f"StorageConfig.read_timeout_sec must be > 0, got {self.read_timeout_sec}"
            )
        if self.multipart_threshold < 1:
            raise ValueError(
                f"StorageConfig.multipart_threshold must be ≥ 1, got {self.multipart_threshold}"
            )
        if self.multipart_chunk_size < 1:
            raise ValueError(
                f"StorageConfig.multipart_chunk_size must be ≥ 1, got {self.multipart_chunk_size}"
            )
        if self.retries < 0:
            raise ValueError(f"StorageConfig.retries must be ≥ 0, got {self.retries}")
