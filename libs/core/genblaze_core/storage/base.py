"""StorageBackend ABC — pluggable object storage interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, BinaryIO, Literal

from genblaze_core._utils import utc_now

_lock_logger = logging.getLogger("genblaze.storage.object_lock")

ObjectLockMode = Literal["GOVERNANCE", "COMPLIANCE"]


@dataclass(frozen=True)
class ObjectLockConfig:
    """Object-lock retention applied to uploaded manifests.

    Genblaze's product promise is cryptographically verified provenance.
    Object Lock is the on-disk expression of that promise — once set, the
    manifest cannot be deleted or overwritten for the retention period.

    Modes:
      * ``"GOVERNANCE"`` (default, recommended) — authorized users holding
        the ``s3:BypassGovernanceRetention`` permission can still delete
        locked objects. Use this for standard audit-trail retention.
      * ``"COMPLIANCE"`` — *no one* can delete the object until retention
        expires, including the account root. Choose only for strict
        regulatory scenarios (e.g. legal hold); a bad retention date
        cannot be shortened.

    ``retain_until`` must be timezone-aware — naive datetimes are rejected
    because S3's interpretation of naive timestamps is ambiguous and we
    refuse to silently accept a 4-year retention with a potentially-wrong
    anchor.

    Backblaze B2 supports Object Lock natively. The bucket must have
    Object Lock enabled at creation time — it cannot be toggled on an
    existing bucket.
    """

    retain_until: datetime
    mode: ObjectLockMode = "GOVERNANCE"

    def __post_init__(self) -> None:
        if self.retain_until.tzinfo is None:
            raise ValueError(
                "ObjectLockConfig.retain_until must be timezone-aware. "
                "Pass a datetime with tzinfo set (e.g. "
                "datetime.now(timezone.utc) + timedelta(days=365))."
            )
        if self.retain_until <= utc_now():
            # Past retention uploads an effectively-unlocked object — surface
            # loudly but don't block, in case the caller is intentionally
            # testing or migrating.
            _lock_logger.warning(
                "ObjectLockConfig.retain_until is in the past (%s); "
                "manifests will be uploaded without effective retention.",
                self.retain_until.isoformat(),
            )

    def to_extra_args(self) -> dict[str, Any]:
        """Serialize into boto3 ``ExtraArgs`` keys for a put/upload call."""
        return {
            "ObjectLockMode": self.mode,
            "ObjectLockRetainUntilDate": self.retain_until,
        }


class KeyStrategy(StrEnum):
    """How to derive object keys in storage.

    HIERARCHICAL groups everything under one run folder::

        {prefix}/runs/{tenant}/{date}/{run_id}/manifest.json
        {prefix}/runs/{tenant}/{date}/{run_id}/assets/{asset_id}.ext

    CONTENT_ADDRESSABLE separates assets and manifests into distinct trees::

        {prefix}/assets/{sha256[:2]}/{sha256[2:4]}/{sha256}.ext
        {prefix}/manifests/{run_id}.json
    """

    HIERARCHICAL = "hierarchical"
    CONTENT_ADDRESSABLE = "content_addressable"


class StorageBackend(ABC):
    """Abstract interface for object storage (S3, B2, GCS, R2, MinIO, etc.)."""

    @abstractmethod
    def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        extra_args: dict[str, Any] | None = None,
    ) -> str:
        """Upload an object. Returns the storage URL.

        ``extra_args`` is a backend-specific passthrough — for the S3 backend it
        maps to boto3's ``ExtraArgs`` (Cache-Control, ServerSideEncryption,
        ObjectLockMode, etc.). Backends that don't recognize a key should
        ignore it rather than raise.
        """
        ...

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Download an object's contents."""
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check whether an object exists."""
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete an object."""
        ...

    @abstractmethod
    def get_url(self, key: str, *, expires_in: int = 3600) -> str:
        """Get a short-lived (possibly pre-signed) URL for the object.

        Use this only for runtime fetches handed to clients. Never persist
        the result — presigned URLs leak credentials (the access key ID is
        embedded in ``X-Amz-Credential``) and break canonical-hash stability.
        Persist :meth:`get_durable_url` instead.
        """
        ...

    @abstractmethod
    def get_durable_url(self, key: str) -> str:
        """Return a credential-free, never-expiring URL safe to persist.

        This is what gets written into ``asset.url`` after a transfer, then
        into manifests, parquet sinks, and embedded media. Implementations
        MUST NOT include any signature, expiry, or credential material.
        """
        ...

    def copy(self, src_key: str, dst_key: str) -> None:
        """Copy an object from src_key to dst_key.

        Used by the pipelined CAS transfer path: content is streamed into a
        temporary key, then copied to its content-addressed final key once
        the hash is known. The default implementation downloads and
        re-uploads — correct but wasteful. Subclasses should override with
        a native server-side copy (S3 ``CopyObject``, B2 ``b2_copy_file``)
        to avoid pointless bandwidth through the client.
        """
        data = self.get(src_key)
        self.put(dst_key, data)

    def close(self) -> None:  # noqa: B027
        """Release any held resources. Override if needed."""
