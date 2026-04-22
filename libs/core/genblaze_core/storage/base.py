"""StorageBackend ABC — pluggable object storage interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, BinaryIO


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
