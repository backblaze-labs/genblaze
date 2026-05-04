"""Discovery cache — single-flight, thread-safe upstream catalog snapshot.

Connectors with a real ``GET /v1/models`` (or equivalent) implement
``BaseProvider.discover_models()``, which fronts a ``_DiscoveryCache``.
The cache:

* memoizes the upstream response per provider instance,
* defaults to a 1-hour TTL so long-running daemons don't silently fly
  blind on a stale snapshot,
* uses single-flight (``threading.Event``) so N concurrent callers across
  threads share one in-flight fetch,
* respects the existing ``RetryPolicy`` machinery for transient errors
  and 429 / Retry-After backoff.

Discovery is *explicit* — never issued at import time, only when a caller
asks. ``BaseProvider.validate_model(refresh=True)`` and the pipeline
preflight phase are the two callers that issue fetches in normal
operation.

Failures fall through to ``DiscoveryStatus.FAILED`` with the cache
untouched. The next caller may retry; meanwhile family patterns and the
permissive fallback continue to serve lookups, so a flaky discovery
endpoint never degrades to a hard outage.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger("genblaze.discovery")

# Default TTL for discovery cache entries. One hour balances long-running
# daemon correctness (catches upstream renames within a reasonable window)
# against short-lived CLI scripts (one fetch per process is plenty). Users
# who want different semantics override per-call via ``max_age_seconds``.
DEFAULT_TTL_SECONDS: float = 3600.0

# Bound the in-flight wait. If a discovery fetch hangs upstream, we don't
# want to deadlock concurrent preflight callers indefinitely. Matches the
# default httpx connect+read budget for catalog endpoints.
SINGLE_FLIGHT_WAIT_SECONDS: float = 30.0


class DiscoveryStatus(StrEnum):
    """Outcome of a single ``_DiscoveryCache.get()`` call."""

    OK = "ok"
    """Cache populated from a fresh upstream fetch (or hit within TTL)."""

    UNSUPPORTED = "unsupported"
    """The provider declared ``DiscoverySupport.NONE`` — no fetch attempted."""

    FAILED = "failed"
    """Network, auth, or transport error during fetch. Cache untouched."""

    STALE = "stale"
    """Cache exists but exceeded the requested ``max_age_seconds`` and a
    refresh failed. The caller may still consume the stale slug set if it
    chooses (the slugs are returned), but should not treat the result as
    authoritative."""


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Snapshot of a provider's upstream catalog at a point in time.

    Returned by ``BaseProvider.discover_models()`` and consumed by
    ``ModelRegistry.validate()`` for the discovery-cache layer of
    resolution. Frozen because callers may share it across threads.
    """

    status: DiscoveryStatus
    slugs: frozenset[str] = field(default_factory=frozenset)
    fetched_at: float | None = None
    """Monotonic time the snapshot was fetched. Used for TTL checks."""

    source_url: str | None = None
    """Upstream catalog URL — for logs, manifests, and debugging."""

    detail: str | None = None
    """Human-readable context for FAILED / STALE outcomes."""

    @classmethod
    def ok(
        cls,
        slugs: frozenset[str] | set[str] | tuple[str, ...],
        *,
        source_url: str | None = None,
    ) -> DiscoveryResult:
        return cls(
            status=DiscoveryStatus.OK,
            slugs=frozenset(slugs),
            fetched_at=time.monotonic(),
            source_url=source_url,
        )

    @classmethod
    def unsupported(cls, *, detail: str | None = None) -> DiscoveryResult:
        return cls(status=DiscoveryStatus.UNSUPPORTED, detail=detail)

    @classmethod
    def failed(
        cls,
        detail: str,
        *,
        source_url: str | None = None,
    ) -> DiscoveryResult:
        return cls(status=DiscoveryStatus.FAILED, detail=detail, source_url=source_url)


class _DiscoveryCache:
    """Per-provider, single-flight, thread-safe discovery cache.

    Concurrency model:

    * State (``_result``, ``_in_flight``) guarded by ``threading.RLock``.
    * Single-flight via ``threading.Event``: at most one in-flight fetch
      per cache. Concurrent callers wait on the same event and read the
      result the winner produced.
    * On fetch failure, ``_result`` is left as the prior cached result
      (could be ``UNSUPPORTED``, a stale ``OK``, or ``None``); the failure
      is returned to the caller but does not poison subsequent reads.

    TTL handling:

    * If a cached ``OK`` is younger than ``max_age_seconds``, it is
      returned immediately (no fetch).
    * If older, a refresh is attempted. On refresh failure, ``STALE`` is
      returned with the prior slug set so callers retain *something*
      useful — better than failing closed for a transient blip.

    The cache does not retry internally — ``RetryPolicy`` is composed by
    the connector's fetcher, which gets one shot to produce a result.
    Cache-level retry would conflict with the upstream's Retry-After
    discipline.
    """

    __slots__ = ("_default_ttl", "_event", "_fetcher", "_in_flight", "_lock", "_result")

    def __init__(
        self,
        fetcher: Callable[[], DiscoveryResult],
        *,
        default_max_age_seconds: float | None = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._fetcher = fetcher
        self._default_ttl = default_max_age_seconds
        self._lock = threading.RLock()
        self._result: DiscoveryResult | None = None
        self._in_flight: bool = False
        self._event: threading.Event | None = None

    def get(self, *, max_age_seconds: float | None = ...) -> DiscoveryResult:  # type: ignore[assignment]
        """Return a fresh-enough discovery snapshot, fetching if needed.

        Args:
            max_age_seconds: Maximum age (seconds, monotonic) of an
                acceptable cached result. ``None`` accepts any cached
                result regardless of age (one-fetch-per-process). Sentinel
                default uses the cache's ``default_max_age_seconds``.

        Returns:
            ``DiscoveryResult`` — never ``None``. Failures surface as
            ``FAILED`` or ``STALE``; callers decide how to react.
        """
        ttl: float | None
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            ttl = self._default_ttl
        else:
            ttl = max_age_seconds

        # Elect either ourselves as the fetcher or identify the in-flight
        # event we should wait on. ``is_fetcher`` disambiguates the two
        # paths so the elected fetcher doesn't accidentally treat its own
        # event as someone else's.
        is_fetcher: bool
        wait_event: threading.Event | None = None
        with self._lock:
            cached = self._result
            if cached is not None and cached.status is DiscoveryStatus.OK:
                if ttl is None or self._is_fresh(cached, ttl):
                    return cached

            if self._in_flight:
                is_fetcher = False
                wait_event = self._event
            else:
                is_fetcher = True
                self._in_flight = True
                self._event = threading.Event()

        if not is_fetcher:
            if wait_event is not None:
                wait_event.wait(timeout=SINGLE_FLIGHT_WAIT_SECONDS)
            with self._lock:
                # Whatever the fetcher produced is now in self._result.
                # Defensive: if the fetcher crashed before populating it,
                # synthesize a FAILED so we never return None.
                return self._result or DiscoveryResult.failed(
                    "Concurrent discovery fetch did not complete in time."
                )

        # Elected fetcher: issue the call outside the lock.
        try:
            result = self._fetcher()
        except Exception as exc:
            logger.warning("discovery fetch raised: %s", exc, exc_info=False)
            result = DiscoveryResult.failed(f"fetcher exception: {exc}")
        finally:
            with self._lock:
                self._in_flight = False
                prior = self._result
                if result.status is DiscoveryStatus.OK or prior is None:
                    self._result = result
                else:
                    # Failed refresh of a previously-good cache: keep the
                    # prior slug set under STALE so callers retain a
                    # usable hint.
                    self._result = DiscoveryResult(
                        status=DiscoveryStatus.STALE,
                        slugs=prior.slugs,
                        fetched_at=prior.fetched_at,
                        source_url=prior.source_url,
                        detail=result.detail,
                    )
                if self._event is not None:
                    self._event.set()
                    self._event = None
                return self._result

    def invalidate(self) -> None:
        """Drop the cached result. Next ``get()`` triggers a fresh fetch."""
        with self._lock:
            self._result = None

    def peek(self) -> DiscoveryResult | None:
        """Return the cached result without triggering a fetch.

        Used by ``ModelRegistry.validate()`` (the non-network entrypoint)
        to consult discovery without forcing a network round-trip.
        """
        with self._lock:
            return self._result

    @staticmethod
    def _is_fresh(result: DiscoveryResult, ttl: float) -> bool:
        if result.fetched_at is None:
            return False
        return (time.monotonic() - result.fetched_at) <= ttl


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "DiscoveryResult",
    "DiscoveryStatus",
    "_DiscoveryCache",
]
