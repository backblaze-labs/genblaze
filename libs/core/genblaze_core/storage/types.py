"""Storage value objects for ``head`` / ``list`` returns.

Phase 2A introduces three frozen dataclasses that the ``StorageBackend``
ABC uses to describe per-object metadata:

* :class:`ObjectMetadata` — what :meth:`StorageBackend.head` returns. Full
  per-object state including user metadata (the ``x-amz-meta-*`` headers).
* :class:`FileEntry` — what each row of :meth:`StorageBackend.list`'s
  ``ListPage.entries`` carries. Subset of ``ObjectMetadata`` matching the
  cheap fields S3 ``ListObjectsV2`` returns natively (no per-key HEAD
  required to populate it).
* :class:`ListPage` — one page of list results. ``next_token`` is
  ``None`` once the listing is exhausted.

All three are :func:`@dataclass(frozen=True, slots=True)` for parity with
:class:`StorageConfig` / :class:`KeyBuilder` — pure value objects, not
wire models, hashable, immutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """Per-object metadata returned by :meth:`StorageBackend.head`.

    The ``etag`` may include surrounding double-quotes when sourced from
    S3 — backends preserve the wire shape rather than stripping. Callers
    comparing etags should account for that or strip explicitly.

    Attributes:
        key: Storage key.
        size: Object size in bytes.
        last_modified: Server-side timestamp; timezone-aware.
        etag: Entity tag (typically the MD5 for single-PUTs; opaque for
            multipart uploads — do not rely on the format).
        content_type: ``Content-Type`` header value if set on upload.
        storage_class: S3 storage class (``STANDARD``, ``GLACIER``, etc.)
            when the backend exposes it. ``None`` for backends that
            don't report a class (e.g. B2 single-tier).
        metadata: User-defined ``x-amz-meta-*`` headers as a flat dict.
            Empty dict when none set; never ``None`` (so callers can
            iterate without a guard).
    """

    key: str
    size: int
    last_modified: datetime
    etag: str
    content_type: str | None = None
    storage_class: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError(f"ObjectMetadata.size must be ≥ 0, got {self.size}")


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Lightweight per-entry shape returned by :meth:`StorageBackend.list`.

    Subset of :class:`ObjectMetadata` covering only what S3
    ``ListObjectsV2`` returns natively — populating the full
    ``ObjectMetadata`` for each entry would require N extra HEAD
    round-trips, which defeats the purpose of pagination.

    Callers that need ``content_type`` or user ``metadata`` for a
    specific entry should call :meth:`StorageBackend.head` on its
    ``key``.
    """

    key: str
    size: int
    last_modified: datetime
    etag: str
    storage_class: str | None = None

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError(f"FileEntry.size must be ≥ 0, got {self.size}")


@dataclass(frozen=True, slots=True)
class ListPage:
    """One page of results from :meth:`StorageBackend.list`.

    Use ``next_token`` to fetch the next page::

        token = None
        while True:
            page = backend.list(prefix="run-", continuation_token=token)
            for entry in page.entries:
                ...
            if page.next_token is None:
                break
            token = page.next_token

    ``entries`` is a ``tuple`` (truly immutable, hashable) so frozen
    semantics carry through; iterate directly or call ``list(page.entries)``
    if you need a mutable copy. ``next_token`` is ``None`` only when the
    listing is exhausted (``IsTruncated=False`` on the S3 response).
    """

    entries: tuple[FileEntry, ...]
    next_token: str | None


@dataclass(frozen=True, slots=True)
class DeleteError:
    """Per-key failure entry from a bulk-delete call.

    Mirrors the shape S3's ``DeleteObjects`` returns in its ``Errors``
    array: ``Code`` + ``Message`` per key. ``code`` matches AWS error
    codes (``AccessDenied``, ``InternalError``, …) when the upstream
    service supplies them; backends that bubble exceptions up under a
    different shape may use ``"backend_error"`` as a synthetic value.
    """

    key: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Outcome of :meth:`StorageBackend.delete_many` or
    :meth:`StorageBackend.delete_prefix`.

    Attributes:
        deleted: Keys that were deleted (or — when ``dry_run=True`` — that
            *would* have been deleted). Order matches the input order
            for ``delete_many``; for ``delete_prefix`` order matches
            the listing order returned by the backend.
        errors: Per-key failures. Empty tuple when every delete
            succeeded (and always empty for ``dry_run=True``, since
            no deletes were attempted).
        dry_run: ``True`` means no upstream calls were made; ``deleted``
            lists the keys the operation *would* have deleted.

    Both ``deleted`` and ``errors`` are tuples (truly immutable +
    hashable, so a ``DeleteResult`` can be stored in a cache or set).
    """

    deleted: tuple[str, ...]
    errors: tuple[DeleteError, ...]
    dry_run: bool

    @property
    def total(self) -> int:
        """``len(deleted) + len(errors)`` — number of keys touched."""
        return len(self.deleted) + len(self.errors)

    @property
    def all_succeeded(self) -> bool:
        """``True`` when zero per-key failures were recorded.

        Always ``True`` for ``dry_run=True`` results.
        """
        return not self.errors


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """One progress event from a long-running storage transfer.

    Passed to the ``progress`` callback on
    :meth:`StorageBackend.put` / :meth:`get` / :meth:`stream`. Callbacks
    are invoked synchronously on the transfer thread — keep them cheap
    (publish to a queue / channel rather than doing I/O).

    Attributes:
        bytes_transferred: Cumulative bytes moved so far. Always
            monotonically non-decreasing within a single transfer.
        total_bytes: Total bytes expected for the transfer when known.
            ``None`` for streaming sources where the total is
            indeterminate (e.g. an upload from a generator).
        operation: ``"put"``, ``"get"``, or ``"stream"`` — the method
            that emitted the event. Useful for callbacks that route to
            different progress UIs per direction.
        key: Storage key the transfer targets.

    Useful patterns::

        def emit_pct(p: TransferProgress) -> None:
            if p.total_bytes is not None:
                pct = 100 * p.bytes_transferred / p.total_bytes
                logger.info("%s %s — %.1f%%", p.operation, p.key, pct)

        backend.put("k", data, progress=emit_pct)
    """

    bytes_transferred: int
    total_bytes: int | None
    operation: str
    key: str


__all__ = [
    "ObjectMetadata",
    "FileEntry",
    "ListPage",
    "DeleteError",
    "DeleteResult",
    "TransferProgress",
]
