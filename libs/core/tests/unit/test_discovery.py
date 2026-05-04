"""Tests for _DiscoveryCache: TTL, single-flight, failure handling."""

from __future__ import annotations

import threading
import time

import pytest
from genblaze_core.providers.discovery import (
    DEFAULT_TTL_SECONDS,
    DiscoveryResult,
    DiscoveryStatus,
    _DiscoveryCache,
)


class TestDiscoveryResult:
    def test_ok_factory(self) -> None:
        r = DiscoveryResult.ok(["model-a", "model-b"], source_url="https://x/v1/models")
        assert r.status is DiscoveryStatus.OK
        assert r.slugs == frozenset({"model-a", "model-b"})
        assert r.source_url == "https://x/v1/models"
        assert r.fetched_at is not None

    def test_unsupported_factory(self) -> None:
        r = DiscoveryResult.unsupported(detail="provider opted out")
        assert r.status is DiscoveryStatus.UNSUPPORTED
        assert r.slugs == frozenset()
        assert r.detail == "provider opted out"

    def test_failed_factory(self) -> None:
        r = DiscoveryResult.failed("network error", source_url="https://x")
        assert r.status is DiscoveryStatus.FAILED
        assert r.detail == "network error"


class TestCacheBasics:
    def test_cold_fetch_populates_cache(self) -> None:
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok(["a", "b"])

        cache = _DiscoveryCache(fetcher)
        result = cache.get()

        assert result.status is DiscoveryStatus.OK
        assert "a" in result.slugs
        assert calls[0] == 1

    def test_warm_fetch_reuses_cache(self) -> None:
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok(["a"])

        cache = _DiscoveryCache(fetcher)
        cache.get()
        cache.get()
        cache.get()

        assert calls[0] == 1, "warm gets should reuse the cached result"

    def test_invalidate_forces_refetch(self) -> None:
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok([f"v{calls[0]}"])

        cache = _DiscoveryCache(fetcher)
        r1 = cache.get()
        cache.invalidate()
        r2 = cache.get()

        assert calls[0] == 2
        assert r1.slugs != r2.slugs

    def test_peek_does_not_fetch(self) -> None:
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok(["a"])

        cache = _DiscoveryCache(fetcher)
        assert cache.peek() is None
        assert calls[0] == 0

        cache.get()
        peek = cache.peek()
        assert peek is not None
        assert peek.status is DiscoveryStatus.OK
        assert calls[0] == 1


class TestTTL:
    def test_default_ttl_constant(self) -> None:
        assert DEFAULT_TTL_SECONDS == 3600.0

    def test_max_age_seconds_zero_forces_refresh(self) -> None:
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok([f"v{calls[0]}"])

        cache = _DiscoveryCache(fetcher)
        cache.get()
        # max_age=0 means "no cached entry is fresh enough" — force refetch.
        cache.get(max_age_seconds=0)
        assert calls[0] == 2

    def test_max_age_none_accepts_any_age(self) -> None:
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok(["a"])

        cache = _DiscoveryCache(fetcher, default_max_age_seconds=0.001)
        cache.get()
        time.sleep(0.05)  # exceed the default TTL
        # max_age=None overrides: accept the cached result regardless.
        cache.get(max_age_seconds=None)
        assert calls[0] == 1


class TestFailureHandling:
    def test_fetcher_exception_returns_failed(self) -> None:
        def fetcher() -> DiscoveryResult:
            raise RuntimeError("boom")

        cache = _DiscoveryCache(fetcher)
        result = cache.get()
        assert result.status is DiscoveryStatus.FAILED
        assert "boom" in (result.detail or "")

    def test_failed_refresh_keeps_prior_slugs_as_stale(self) -> None:
        # First fetch succeeds; subsequent fetch fails. The cache should
        # downgrade to STALE while preserving the prior slug set so
        # callers retain a usable hint.
        first = [True]
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            if first[0]:
                first[0] = False
                return DiscoveryResult.ok(["a", "b"])
            return DiscoveryResult.failed("transient")

        cache = _DiscoveryCache(fetcher, default_max_age_seconds=0.001)
        r1 = cache.get()
        assert r1.status is DiscoveryStatus.OK
        time.sleep(0.05)
        r2 = cache.get()
        assert r2.status is DiscoveryStatus.STALE
        assert r2.slugs == frozenset({"a", "b"})
        assert "transient" in (r2.detail or "")


class TestSingleFlight:
    def test_concurrent_callers_share_one_fetch(self) -> None:
        calls = [0]
        gate = threading.Event()

        def slow_fetcher() -> DiscoveryResult:
            calls[0] += 1
            gate.wait(timeout=2.0)
            return DiscoveryResult.ok(["x"])

        cache = _DiscoveryCache(slow_fetcher)
        results: list[DiscoveryResult] = []
        threads = [threading.Thread(target=lambda: results.append(cache.get())) for _ in range(50)]
        for t in threads:
            t.start()
        # Give threads a moment to enqueue, then release the fetcher.
        time.sleep(0.05)
        gate.set()
        for t in threads:
            t.join(timeout=5.0)

        assert calls[0] == 1, f"expected 1 fetch under contention, got {calls[0]}"
        assert len(results) == 50
        assert all(r.status is DiscoveryStatus.OK for r in results)

    def test_sequential_invalidations_serialize(self) -> None:
        # invalidate() between two get() calls produces two distinct
        # fetches — single-flight only deduplicates *concurrent* calls.
        calls = [0]

        def fetcher() -> DiscoveryResult:
            calls[0] += 1
            return DiscoveryResult.ok([f"v{calls[0]}"])

        cache = _DiscoveryCache(fetcher)
        cache.get()
        cache.invalidate()
        cache.get()
        assert calls[0] == 2
