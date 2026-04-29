"""Sync↔async parity for ``StorageBackend`` ABC default implementations.

Phase 0 ships only the 6 existing methods plus their async pairs. Native
``aioboto3`` paths and the missing-primitive method set land in later
phases; this test pins the contract that every existing sync method has
a coroutine pair, and that the default impl threadpool-delegates to the
sync method on a concrete (test-only) backend.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from genblaze_core.storage.base import StorageBackend


class _FakeBackend(StorageBackend):
    """Minimal in-memory backend for parity tests.

    Records each sync call so the async-pair tests can assert delegation
    actually happened (rather than silently returning the default).
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))

    def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
        self._record("put", (key,), {"content_type": content_type})
        if isinstance(data, (bytes, bytearray)):
            self._store[key] = bytes(data)
        else:
            self._store[key] = data.read()
        return f"https://example.test/{key}"

    def get(self, key):
        self._record("get", (key,), {})
        return self._store[key]

    def exists(self, key):
        self._record("exists", (key,), {})
        return key in self._store

    def delete(self, key):
        self._record("delete", (key,), {})
        self._store.pop(key, None)

    def get_url(self, key, *, expires_in=3600):
        self._record("get_url", (key,), {"expires_in": expires_in})
        return f"https://example.test/{key}?expires={expires_in}"

    def get_durable_url(self, key):
        self._record("get_durable_url", (key,), {})
        return f"https://example.test/{key}"


# ---------------------------------------------------------------------------
# ABC shape — every sync method has a matching async pair.
# ---------------------------------------------------------------------------

# (sync_name, async_name) — Phase 0 surface only. Updated when Phase 2
# lands head/list/get_range/etc.
_PAIRS = [
    ("put", "aput"),
    ("get", "aget"),
    ("exists", "aexists"),
    ("delete", "adelete"),
    ("get_url", "aget_url"),
    ("get_durable_url", "aget_durable_url"),
    ("copy", "acopy"),
]


@pytest.mark.parametrize("sync_name,async_name", _PAIRS)
def test_async_pair_exists_on_abc(sync_name: str, async_name: str) -> None:
    sync = getattr(StorageBackend, sync_name, None)
    aw = getattr(StorageBackend, async_name, None)
    assert sync is not None, f"StorageBackend missing sync method {sync_name!r}"
    assert aw is not None, f"StorageBackend missing async pair {async_name!r}"
    assert inspect.iscoroutinefunction(aw), (
        f"StorageBackend.{async_name} must be `async def` — got {aw!r}"
    )


# ---------------------------------------------------------------------------
# Default async impls delegate to the sync method on a concrete subclass.
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_aput_delegates_to_put() -> None:
    backend = _FakeBackend()
    url = asyncio.run(backend.aput("k1", b"hello", content_type="text/plain"))
    assert url == "https://example.test/k1"
    assert backend.calls[0][0] == "put"
    assert backend._store["k1"] == b"hello"


def test_aget_delegates_to_get() -> None:
    backend = _FakeBackend()
    backend._store["k1"] = b"world"
    out = asyncio.run(backend.aget("k1"))
    assert out == b"world"
    assert backend.calls[0][0] == "get"


def test_aexists_delegates_to_exists() -> None:
    backend = _FakeBackend()
    backend._store["yes"] = b""
    assert asyncio.run(backend.aexists("yes")) is True
    assert asyncio.run(backend.aexists("no")) is False


def test_adelete_delegates_to_delete() -> None:
    backend = _FakeBackend()
    backend._store["zap"] = b"x"
    asyncio.run(backend.adelete("zap"))
    assert "zap" not in backend._store
    assert backend.calls[0][0] == "delete"


def test_aget_url_passes_expires_in() -> None:
    backend = _FakeBackend()
    url = asyncio.run(backend.aget_url("k1", expires_in=42))
    assert url.endswith("?expires=42")
    assert backend.calls[0][2]["expires_in"] == 42


def test_aget_durable_url_delegates() -> None:
    backend = _FakeBackend()
    url = asyncio.run(backend.aget_durable_url("k1"))
    assert url == "https://example.test/k1"


def test_acopy_delegates_to_copy() -> None:
    backend = _FakeBackend()
    backend._store["src"] = b"copied"
    asyncio.run(backend.acopy("src", "dst"))
    # _FakeBackend doesn't override copy; the ABC default downloads+uploads.
    assert backend._store["dst"] == b"copied"
