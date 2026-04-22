"""StorageBackend ABC — pluggable object storage interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, BinaryIO, Literal

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

    Backblaze B2 supports Object Lock natively. The bucket must have
    Object Lock enabled at creation time — it cannot be toggled on an
    existing bucket.
    """

    retain_until: datetime
    mode: ObjectLockMode = "GOVERNANCE"

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
        """Get a (possibly pre-signed) URL for the object."""
        ...

    def close(self) -> None:  # noqa: B027
        """Release any held resources. Override if needed."""
