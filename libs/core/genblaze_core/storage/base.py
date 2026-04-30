"""StorageBackend ABC — pluggable object storage interface."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, BinaryIO, Literal

from genblaze_core._utils import utc_now
from genblaze_core.storage.types import (  # noqa: F401
    DeleteError,
    DeleteResult,
    FileEntry,
    ListPage,
    ObjectMetadata,
)

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
        """Upload an object. Returns the storage **key** the object was written
        under (the same value the caller passed in for backends that don't
        rewrite keys; see :meth:`get_durable_url` to convert a key to a URL).

        Returning a presigned URL here was the historic shape; it leaked the
        access-key-id (`X-Amz-Credential`) into anything that persisted the
        return — logs, manifests, DB rows — and broke canonical-hash stability
        for content-addressable layouts (the signature rotates per call).
        Persist :meth:`get_durable_url(key) <get_durable_url>` instead, or
        opt into a presigned URL via the dedicated presigned methods on
        backends that expose them.

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

    def key_from_url(self, url: str) -> str | None:
        """Inverse of :meth:`get_durable_url` — extract a storage key.

        Returns:
            The key when ``url`` was produced by this backend (round-trips
            with :meth:`get_durable_url`). Returns ``None`` for URLs that
            clearly belong to a different backend (different host, different
            bucket, or unparseable) — that's the "not mine" signal, distinct
            from "not implemented".

        Raises:
            NotImplementedError: in the default implementation. Backends that
                lack a well-defined inverse must opt in; ``None`` is reserved
                for foreign URLs only, never for missing implementations.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement key_from_url")

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

    # ------------------------------------------------------------------
    # Phase 2A read primitives — head / list / get_range / stream.
    #
    # All four are concrete-with-NotImplementedError defaults so existing
    # third-party ``StorageBackend`` subclasses that predate Phase 2A
    # don't break at import. Backends opt in by overriding. The S3
    # backend implements all four natively.
    # ------------------------------------------------------------------

    def head(self, key: str) -> ObjectMetadata | None:
        """Return per-object metadata, or ``None`` if the key is missing.

        Tolerant of 404 AND 403 (parity with :meth:`exists` for scoped
        application keys that get 403 on non-existent reads). Other
        errors raise :class:`StorageError`.

        Default impl raises :class:`NotImplementedError`. Backends that
        support HEAD-style introspection override.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement head()")

    def list(
        self,
        prefix: str = "",
        *,
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ) -> ListPage:
        """List keys under ``prefix`` (one page).

        Pagination is opt-in via ``continuation_token``; pass
        ``page.next_token`` from a prior call to fetch the next page.
        ``page.next_token`` is ``None`` when the listing is exhausted.

        Default impl raises :class:`NotImplementedError`. Backends with
        native list APIs (S3 ``ListObjectsV2``, B2 ``b2_list_file_names``)
        override.

        Args:
            prefix: Only return keys starting with this prefix.
            max_keys: Page size cap. S3 silently caps at 1000; backends
                may reduce further.
            continuation_token: Cursor from a prior page. ``None`` for
                the first page.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement list()")

    def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        """Download a byte range from the object.

        Args:
            key: Storage key.
            offset: Byte offset (inclusive). Must be ≥ 0.
            length: Number of bytes to read. Must be ≥ 0; ``0`` returns
                an empty bytes string without contacting the backend.

        Default impl raises :class:`NotImplementedError`. A correctness-
        preserving fallback (``self.get(key)[offset:offset+length]``)
        would download the whole object — defeating the point — so
        backends must explicitly opt in by implementing range reads.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement get_range()")

    def stream(self, key: str, *, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        """Lazily download an object, yielding ``chunk_size``-sized chunks.

        Use this for objects too large to load into memory in one call.
        The iterator must be consumed (or closed) to release the
        underlying response — most backends keep an HTTP connection in
        the pool until exhaustion.

        Default impl raises :class:`NotImplementedError` — same reasoning
        as :meth:`get_range`: a fallback chunking ``self.get`` would
        defeat the streaming contract.

        Args:
            key: Storage key.
            chunk_size: Bytes per yielded chunk. Must be ≥ 1. Larger
                chunks reduce per-iteration overhead; smaller chunks
                reduce peak memory.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement stream()")

    # ------------------------------------------------------------------
    # Phase 2B bulk-delete primitives.
    # ------------------------------------------------------------------

    def delete_many(self, keys: Sequence[str], *, dry_run: bool = False) -> DeleteResult:
        """Delete a batch of keys.

        Backends are expected to issue a single bulk-delete API call per
        chunk (S3 ``DeleteObjects`` is capped at 1000 keys per request);
        the per-key wire shape lets each delete fail independently —
        partial-failure callers inspect ``result.errors``.

        Args:
            keys: Keys to delete. Empty list returns an empty result
                without contacting the backend.
            dry_run: When ``True``, no upstream calls are made;
                the returned :class:`DeleteResult` lists every key in
                ``deleted`` so the caller can preview the operation.
                Default ``False`` — caller passes the keys explicitly,
                so dry-run-by-default would be more friction than
                safety.

        Default impl raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement delete_many()")

    def delete_prefix(self, prefix: str, *, dry_run: bool = True) -> DeleteResult:
        """Delete every key under a prefix. **Defaults to dry-run.**

        Walks :meth:`list` pages and issues bulk deletes per page, so
        memory stays bounded even for prefixes matching millions of
        keys. The default ``dry_run=True`` is the safety asymmetry:
        ``delete_many(keys)`` takes an explicit list (caller knows what
        they're deleting) while ``delete_prefix(prefix)`` takes a
        pattern that could match more than the caller intended — so
        the SDK demands an explicit ``dry_run=False`` to actually
        delete.

        Args:
            prefix: Non-empty key prefix. Empty / whitespace-only
                prefixes raise ``ValueError`` — passing ``""`` would
                match every object in the bucket, which is virtually
                always a bug. Callers who genuinely want bucket-wide
                deletes should iterate :meth:`list` themselves and
                feed the keys into :meth:`delete_many`.
            dry_run: ``True`` (default) walks the listing without
                deleting; ``False`` actually deletes.

        Default impl raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement delete_prefix()")

    # ------------------------------------------------------------------
    # Async surface — every sync method has a coroutine pair.
    #
    # Default implementations delegate to the sync method via
    # ``asyncio.to_thread``. Backends that want native async (e.g.
    # ``aioboto3``) override these directly. Threadpool delegation is
    # safe for one-shot operations (put/get/exists/delete/copy/get_url);
    # streaming primitives (``stream``, ``list``) are deliberately NOT
    # added to this ABC in Phase 0 — wrapping a sync iterator into an
    # ``AsyncIterator`` via threadpool buffers the entire result and
    # lies about back-pressure semantics. Those land natively in Phase 2.
    # ------------------------------------------------------------------

    async def aput(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        extra_args: dict[str, Any] | None = None,
    ) -> str:
        """Async pair of :meth:`put`. Default delegates to the sync impl."""
        return await asyncio.to_thread(
            self.put,
            key,
            data,
            content_type=content_type,
            metadata=metadata,
            extra_args=extra_args,
        )

    async def aget(self, key: str) -> bytes:
        """Async pair of :meth:`get`. Default delegates to the sync impl."""
        return await asyncio.to_thread(self.get, key)

    async def aexists(self, key: str) -> bool:
        """Async pair of :meth:`exists`. Default delegates to the sync impl."""
        return await asyncio.to_thread(self.exists, key)

    async def adelete(self, key: str) -> None:
        """Async pair of :meth:`delete`. Default delegates to the sync impl."""
        await asyncio.to_thread(self.delete, key)

    async def aget_url(self, key: str, *, expires_in: int | None = None, **kwargs: Any) -> str:
        """Async pair of :meth:`get_url`. Default delegates to the sync impl.

        ``expires_in`` defaults to ``None`` (NOT 3600) so that backends
        using a sentinel-based default (e.g. :class:`S3StorageBackend`'s
        ``URLPolicy.PUBLIC`` conflict detection) see "caller didn't pass"
        rather than "caller passed 3600". Pass an int to force an explicit
        value through.

        ``**kwargs`` forwards backend-specific options (e.g.
        ``policy=URLPolicy.PUBLIC`` on the S3 backend) without coupling
        the ABC to connector-side types.

        Note: presigned-URL signing is local crypto in boto3, so threadpool
        wrapping is overkill — backends with native async signing should
        override and skip the dispatch.
        """
        if expires_in is None:
            return await asyncio.to_thread(self.get_url, key, **kwargs)
        return await asyncio.to_thread(self.get_url, key, expires_in=expires_in, **kwargs)

    async def aget_durable_url(self, key: str) -> str:
        """Async pair of :meth:`get_durable_url`.

        Pure-string transformation in most backends; threadpool wrap is
        purely for symmetry with the rest of the async surface.
        """
        return await asyncio.to_thread(self.get_durable_url, key)

    async def acopy(self, src_key: str, dst_key: str) -> None:
        """Async pair of :meth:`copy`. Default delegates to the sync impl."""
        await asyncio.to_thread(self.copy, src_key, dst_key)

    async def ahead(self, key: str) -> ObjectMetadata | None:
        """Async pair of :meth:`head`. Default delegates to the sync impl."""
        return await asyncio.to_thread(self.head, key)

    async def aget_range(self, key: str, *, offset: int, length: int) -> bytes:
        """Async pair of :meth:`get_range`. Default delegates to the sync impl."""
        return await asyncio.to_thread(self.get_range, key, offset=offset, length=length)

    # ``alist`` and ``astream`` are deliberately omitted from the ABC.
    # Threadpool-wrapping a sync iterator into an ``AsyncIterator`` either
    # buffers the entire result (defeating streaming back-pressure) or
    # spins up a queue per call (overkill for a default). Phase 3 of the
    # storage-backend hardening tranche introduces native async via
    # ``aioboto3`` for these specifically.

    async def adelete_many(self, keys: Sequence[str], *, dry_run: bool = False) -> DeleteResult:
        """Async pair of :meth:`delete_many`. Default delegates to sync."""
        return await asyncio.to_thread(self.delete_many, keys, dry_run=dry_run)

    async def adelete_prefix(self, prefix: str, *, dry_run: bool = True) -> DeleteResult:
        """Async pair of :meth:`delete_prefix`. Default delegates to sync.

        Note: ``delete_prefix`` itself walks ``list()`` pages, so a native
        async backend (Phase 3) gets a free win by overriding ``alist``
        and re-using a generic page-walker rather than threadpool-wrapping
        the whole walk.
        """
        return await asyncio.to_thread(self.delete_prefix, prefix, dry_run=dry_run)
