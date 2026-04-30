"""Unit tests for ``genblaze_core.storage.types``.

Pin the contracts of the three Phase 2A value objects: frozen,
hashable, validation behavior.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from genblaze_core.storage.types import (
    DeleteError,
    DeleteResult,
    FileEntry,
    ListPage,
    ObjectMetadata,
    TransferProgress,
)

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ObjectMetadata
# ---------------------------------------------------------------------------


class TestObjectMetadata:
    def test_basic_construction(self) -> None:
        m = ObjectMetadata(
            key="k",
            size=42,
            last_modified=_NOW,
            etag='"abc"',
            content_type="image/png",
            storage_class="STANDARD",
            metadata={"x-app": "demo"},
        )
        assert m.key == "k"
        assert m.size == 42
        assert m.metadata == {"x-app": "demo"}

    def test_defaults(self) -> None:
        m = ObjectMetadata(key="k", size=0, last_modified=_NOW, etag="")
        # Optional fields default to None / empty dict.
        assert m.content_type is None
        assert m.storage_class is None
        assert m.metadata == {}

    def test_metadata_default_is_independent_dict(self) -> None:
        """Default-factory dict must NOT be shared across instances —
        otherwise a mutation on one would leak to the next."""
        a = ObjectMetadata(key="k1", size=0, last_modified=_NOW, etag="")
        b = ObjectMetadata(key="k2", size=0, last_modified=_NOW, etag="")
        assert a.metadata is not b.metadata

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="size must be ≥ 0"):
            ObjectMetadata(key="k", size=-1, last_modified=_NOW, etag="")

    def test_frozen(self) -> None:
        m = ObjectMetadata(key="k", size=0, last_modified=_NOW, etag="")
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.size = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FileEntry
# ---------------------------------------------------------------------------


class TestFileEntry:
    def test_basic_construction(self) -> None:
        e = FileEntry(key="k", size=10, last_modified=_NOW, etag='"x"')
        assert e.key == "k"
        assert e.storage_class is None  # optional default

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="size must be ≥ 0"):
            FileEntry(key="k", size=-1, last_modified=_NOW, etag="")

    def test_frozen_and_hashable(self) -> None:
        """FileEntry has only hashable fields → must be hashable for use as
        dict keys / set members in caller-side bookkeeping."""
        a = FileEntry(key="k", size=10, last_modified=_NOW, etag='"x"')
        b = FileEntry(key="k", size=10, last_modified=_NOW, etag='"x"')
        c = FileEntry(key="other", size=10, last_modified=_NOW, etag='"x"')
        assert hash(a) == hash(b)
        assert hash(a) != hash(c)
        # Set semantics work.
        assert len({a, b, c}) == 2


# ---------------------------------------------------------------------------
# ListPage
# ---------------------------------------------------------------------------


class TestListPage:
    def test_empty_page(self) -> None:
        page = ListPage(entries=(), next_token=None)
        assert page.entries == ()
        assert page.next_token is None

    def test_with_entries_and_token(self) -> None:
        e = FileEntry(key="k", size=1, last_modified=_NOW, etag='"x"')
        page = ListPage(entries=(e,), next_token="cursor-1")
        assert page.entries == (e,)
        assert page.next_token == "cursor-1"  # noqa: S105 — pagination cursor, not a password

    def test_entries_is_tuple_immutable(self) -> None:
        """Tuple, not list — we don't want mutation to leak across callers."""
        e = FileEntry(key="k", size=1, last_modified=_NOW, etag='"x"')
        page = ListPage(entries=(e,), next_token=None)
        assert isinstance(page.entries, tuple)
        # Frozen dataclass blocks reassignment.
        with pytest.raises(dataclasses.FrozenInstanceError):
            page.entries = (e, e)  # type: ignore[misc]

    def test_hashable(self) -> None:
        """Tuple-of-FileEntry is hashable; FileEntry is hashable; so ListPage
        is hashable end-to-end. Useful for caching list-page results."""
        e = FileEntry(key="k", size=1, last_modified=_NOW, etag='"x"')
        page_a = ListPage(entries=(e,), next_token=None)
        page_b = ListPage(entries=(e,), next_token=None)
        assert hash(page_a) == hash(page_b)


# ---------------------------------------------------------------------------
# DeleteError + DeleteResult
# ---------------------------------------------------------------------------


class TestDeleteError:
    def test_basic_construction(self) -> None:
        err = DeleteError(key="k", code="AccessDenied", message="forbidden")
        assert err.key == "k"
        assert err.code == "AccessDenied"
        assert err.message == "forbidden"

    def test_frozen_and_hashable(self) -> None:
        a = DeleteError(key="k", code="X", message="m")
        b = DeleteError(key="k", code="X", message="m")
        assert hash(a) == hash(b)
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.code = "Y"  # type: ignore[misc]


class TestDeleteResult:
    def test_basic_construction(self) -> None:
        r = DeleteResult(deleted=("a", "b"), errors=(), dry_run=False)
        assert r.deleted == ("a", "b")
        assert r.errors == ()
        assert r.dry_run is False

    def test_total_property(self) -> None:
        err = DeleteError(key="c", code="X", message="m")
        r = DeleteResult(deleted=("a", "b"), errors=(err,), dry_run=False)
        assert r.total == 3

    def test_all_succeeded_property(self) -> None:
        clean = DeleteResult(deleted=("a",), errors=(), dry_run=False)
        partial = DeleteResult(
            deleted=("a",),
            errors=(DeleteError(key="b", code="X", message="m"),),
            dry_run=False,
        )
        assert clean.all_succeeded is True
        assert partial.all_succeeded is False

    def test_dry_run_always_succeeds(self) -> None:
        """``dry_run=True`` results never carry errors (no network calls
        were made), so ``all_succeeded`` is trivially True."""
        r = DeleteResult(deleted=("a", "b"), errors=(), dry_run=True)
        assert r.all_succeeded is True
        assert r.total == 2

    def test_frozen_and_hashable(self) -> None:
        r = DeleteResult(deleted=("a",), errors=(), dry_run=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.dry_run = True  # type: ignore[misc]
        # Hashable: tuple fields are immutable so the dataclass hash works.
        assert hash(r) == hash(DeleteResult(deleted=("a",), errors=(), dry_run=False))


# ---------------------------------------------------------------------------
# TransferProgress
# ---------------------------------------------------------------------------


class TestTransferProgress:
    def test_basic_construction(self) -> None:
        p = TransferProgress(
            bytes_transferred=512,
            total_bytes=1024,
            operation="put",
            key="k",
        )
        assert p.bytes_transferred == 512
        assert p.total_bytes == 1024
        assert p.operation == "put"
        assert p.key == "k"

    def test_total_can_be_none_for_streamed_sources(self) -> None:
        """``total_bytes=None`` is the documented signal that the total
        is unknown — required for arbitrary BinaryIO uploads where the
        size would only become known by draining the stream."""
        p = TransferProgress(bytes_transferred=42, total_bytes=None, operation="put", key="k")
        assert p.total_bytes is None

    def test_frozen_and_hashable(self) -> None:
        a = TransferProgress(bytes_transferred=1, total_bytes=10, operation="get", key="k")
        b = TransferProgress(bytes_transferred=1, total_bytes=10, operation="get", key="k")
        assert hash(a) == hash(b)
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.bytes_transferred = 2  # type: ignore[misc]
