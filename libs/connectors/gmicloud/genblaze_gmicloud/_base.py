"""Shared base for all GMICloud media providers (video, image, audio).

Owns auth, HTTP client lifecycle, and the common poll() implementation
since all modalities use the same async request queue API.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from typing import Any

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers import (
    DiscoverySupport,
    LiveProbeResult,
    ValidationOutcome,
    ValidationResult,
    ValidationSource,
)
from genblaze_core.providers.base import BaseProvider, SubmitResult
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.retry import RetryPolicy, retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_gmicloud_error

_DEFAULT_BASE_URL = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"

_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})

# Default connector-side ceiling on how long a single request may sit in a
# non-terminal state before we fail it (#262). GMICloud occasionally leaves a
# request wedged in ``queued``/``processing`` indefinitely; the core poll loop's
# only backstop is the caller-supplied ``config["timeout"]``, which long video
# runs set very large (or leave unset), so a wedged job would otherwise poll
# forever. 30 minutes is well beyond any real Seedance/Veo/Kling completion.
_DEFAULT_MAX_POLL_SECONDS = 1800.0

# Upper bound on the ``_poll_first_seen`` deadline map so a long-lived provider
# that accumulates many wedged/abandoned request ids can't grow it unbounded.
# Entries are removed on a terminal poll; this FIFO cap bounds the pathological
# case where jobs only ever fail via the ceiling. 512 dwarfs any realistic
# in-flight fan-out.
_POLL_FIRST_SEEN_MAX = 512


def _is_inference_endpoint_shaped(url: str) -> bool:
    """True when ``url`` has the shape of GMICloud's chat/inference endpoint.

    GMICloud's OpenAI-compatible inference endpoint (``chat.py``'s own
    default, ``https://api.gmi-serving.com/v1``) always terminates at
    ``/v1``. The request-queue endpoint this module talks to always has
    additional path segments after ``/v1`` (e.g. the default
    ``.../v1/ie/requestqueue/apikey``, or a VPC proxy's own routing
    suffix). A bare ``/v1`` ending is therefore a reliable signal that
    ``GMI_BASE_URL``/``base_url=`` was pointed at the wrong surface — see
    the guard in ``GMICloudBase.__init__`` (#193).
    """
    return url.rstrip("/").endswith("/v1")


# Legacy flat outcome keys — kept as defensive fallbacks while GMICloud
# completes its migration to the ``media_urls`` envelope.
_LEGACY_URL_KEYS = ("video_url", "image_url", "audio_url", "url")


def extract_media_urls(outcome: dict, *, image_fallback: bool = False) -> list[str]:
    """Pull all asset URLs from a GMICloud request outcome.

    Priority: ``media_urls[*].url`` (current shape) → flat ``*_url`` keys
    (legacy shape, single-item list) → ``thumbnail_image_url`` for image
    modality only. Returns an empty list when nothing is available.
    """
    urls: list[str] = []
    media_urls = outcome.get("media_urls")
    if isinstance(media_urls, list):
        for entry in media_urls:
            if isinstance(entry, dict):
                url = entry.get("url")
                if url:
                    urls.append(str(url))
            elif isinstance(entry, str) and entry:
                urls.append(entry)
    if urls:
        return urls
    # Legacy fallbacks only kick in when the primary envelope is empty.
    for key in _LEGACY_URL_KEYS:
        v = outcome.get(key)
        if v:
            return [str(v)]
    if image_fallback:
        thumb = outcome.get("thumbnail_image_url")
        if thumb:
            return [str(thumb)]
    return []


def extract_media_url(outcome: dict, *, image_fallback: bool = False) -> str | None:
    """Return the first asset URL from a GMICloud outcome (video / audio path).

    Thin wrapper over ``extract_media_urls`` for single-output modalities.
    """
    urls = extract_media_urls(outcome, image_fallback=image_fallback)
    return urls[0] if urls else None


def unwrap_error_body(text: str) -> str:
    """Extract inner ``{"error": "..."}`` text from a JSON error body.

    Returns the raw text if the body isn't JSON or doesn't have an ``error``
    key. Prevents confusing double-wrapped messages like
    ``'GMICloud submit failed (500): {"error":"Backend error (400)..."}'``.
    """
    stripped = text.strip()
    if not stripped:
        return text
    try:
        body = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if isinstance(body, dict):
        inner = body.get("error") or body.get("message") or body.get("detail")
        if isinstance(inner, str) and inner:
            return inner
    return text


class GMICloudBase(BaseProvider):
    """Base class for GMICloud providers — handles auth, HTTP client, and polling.

    All GMICloud media APIs share the same request queue, auth, and poll
    lifecycle. Subclasses implement ``get_capabilities()``, ``submit()``,
    and ``fetch_output()`` for their specific modality.

    GMICloud has no authoritative ``GET /models`` endpoint, so this base
    class declares ``DiscoverySupport.PARTIAL``. Slug liveness is
    confirmed via the empty-payload probe attached to each
    ``ModelFamily`` — see ``_probe.empty_payload_request_probe`` and
    ``_invoke_family_probe`` below.

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
            Ignored when ``http_client`` is supplied.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
            Ignored when ``http_client`` is supplied.
        max_poll_seconds: Connector-side ceiling on how long a single request
            may remain in a non-terminal (``queued``/``processing``) state
            before ``poll()`` fails it with a retryable ``TIMEOUT`` error
            (default 1800). This is independent of — and a hard backstop for —
            the caller-supplied per-step ``timeout``: a wedged upstream job
            terminates here even when the caller set a very large or unbounded
            ``timeout`` for long video (#262). Pass ``None`` to disable and rely
            solely on the caller's ``timeout``.
        base_url: Override the request-queue base URL. Falls back to the
            GMI_BASE_URL env var, then the canonical production URL.
            Ignored when ``http_client`` is supplied.
        http_client: Pre-built ``httpx.Client`` to inject. Must have auth
            headers and base URL already configured. Enables sharing one
            client across multiple provider instances (video + image +
            audio) in multi-modality pipelines. When supplied, the base
            class will never close it — lifecycle is the caller's.
    """

    discovery_support = DiscoverySupport.PARTIAL
    """GMICloud's request-queue surface has no ``GET /models``. The
    family-attached empty-payload probe is the authoritative liveness
    signal — see ``_invoke_family_probe`` below and ``_probe.py``."""

    _entitlement_gated_slugs: frozenset[str] = frozenset()
    """Slugs known to require GMICloud account entitlement beyond a valid
    API key (e.g. gated third-party models). The empty-payload probe (see
    ``_probe.py``) can only prove a slug exists in GMICloud's catalog —
    GMICloud validates payload *shape* before account *entitlement*, so
    the probe's deliberately-empty payload never reaches the entitlement
    gate a real, well-formed submit hits. That lets ``validate_model()``
    report a slug as fully confirmed (``OK_AUTHORITATIVE``) when in fact
    submitting it 404s with "you do not have access" — preflight passes,
    the job dies at dispatch (#193). ``validate_model()`` below re-grades
    these specific, known-gated slugs to ``OK_PROVISIONAL`` so the result
    is honest about what the probe actually proved. Populated per-modality
    by subclasses; empty by default (most slugs are not gated, and this
    connector has no way to discover which are without a curated list)."""

    def _invoke_family_probe(self, probe: Any, model_id: str) -> LiveProbeResult:
        """Forward the family probe with this provider's ``httpx.Client``."""
        return probe(model_id, http=self._get_http_client())

    def __init__(
        self,
        api_key: str | None = None,
        *,
        poll_interval: float = 5.0,
        http_timeout: float = 120.0,
        max_poll_seconds: float | None = _DEFAULT_MAX_POLL_SECONDS,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ):
        # Forward models= to BaseProvider so the documented per-instance
        # registry override actually takes effect (closes feedback P0-03).
        # probe_cache_* kwargs let operators tune cache semantics per
        # deployment without process-global class-attribute mutation.
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self._max_poll_seconds = max_poll_seconds
        # Monotonic timestamp of the first poll seen for each in-flight
        # prediction id, used to enforce ``max_poll_seconds``. Guarded by a
        # lock because one provider instance is shared across a fan-out
        # (sync ThreadPoolExecutor, or async via ``asyncio.to_thread``) — same
        # concurrency contract as the core poll-result cache.
        self._poll_first_seen: dict[str, float] = {}
        self._poll_first_seen_lock = threading.Lock()
        self._api_key: str | None = api_key or os.environ.get("GMI_API_KEY")
        self._http_timeout = http_timeout
        self._base_url: str = base_url or os.environ.get("GMI_BASE_URL") or _DEFAULT_BASE_URL
        # GMI_BASE_URL reads as a general GMICloud override but is only ever
        # consulted here (chat() has its own hardcoded default and never
        # reads it) — pointing it at the serving/inference URL silently
        # 404s every image/video/audio model while chat() keeps working
        # (#193). Skip the check when http_client is supplied: base_url is
        # documented as ignored in that path, and raising here would
        # contradict that contract for callers who inject their own client.
        if http_client is None and _is_inference_endpoint_shaped(self._base_url):
            source = "base_url=" if base_url else "GMI_BASE_URL"
            raise ProviderError(
                f"{source} {self._base_url!r} looks like GMICloud's chat/inference "
                f"endpoint (path ends in '/v1'), not the request-queue endpoint "
                f"{type(self).__name__} talks to. GMI_BASE_URL is read only by the "
                "video/image/audio queue providers — chat() has its own default "
                "and never consults it — so this would silently 404 every submit "
                "on this provider while chat() keeps working fine. Leave it unset "
                f"to use the default ({_DEFAULT_BASE_URL!r}), or point it at a "
                "queue proxy/VPC URL with the queue's own path suffix "
                "(e.g. '.../v1/ie/requestqueue/apikey').",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        self._http_client: httpx.Client | None = http_client
        self._owns_client: bool = http_client is None

    def _get_http_client(self) -> httpx.Client:
        """Lazy-create httpx client with API key Bearer auth."""
        if self._http_client is None:
            if not self._api_key:
                raise ProviderError(
                    "No API key found. Set GMI_API_KEY env var or pass api_key=.",
                    error_code=ProviderErrorCode.AUTH_FAILURE,
                )
            self._http_client = httpx.Client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._http_timeout,
            )
            self._owns_client = True
        return self._http_client

    def close(self) -> None:
        """Release connection-pool resources for internally-created clients.

        No-op when an external ``http_client`` was injected — the caller owns
        that client's lifecycle.
        """
        if self._http_client is not None and self._owns_client:
            self._http_client.close()
            self._http_client = None

    def _submit_request(self, model: str, payload: dict) -> SubmitResult:
        """POST a generation request and return a SubmitResult.

        ``model`` is the caller-supplied id; it gets resolved to the canonical
        (case-correct) GMICloud slug before being sent on the wire.
        """
        canonical = self._models.resolve_canonical(model)
        client = self._get_http_client()
        resp = client.post("/requests", json={"model": canonical, "payload": payload})
        if resp.status_code >= 400:
            inner = unwrap_error_body(resp.text)
            raise ProviderError(
                f"GMICloud submit failed ({resp.status_code}): {inner}",
                error_code=map_gmicloud_error(Exception(inner), resp.status_code),
                retry_after=retry_after_from_response(resp),
            )
        data = resp.json()
        request_id = data.get("request_id") or data.get("id")
        return SubmitResult(prediction_id=request_id, estimated_seconds=30.0)

    def _clear_poll_deadline(self, key: str) -> None:
        """Forget the stall deadline for a request that has finished polling."""
        with self._poll_first_seen_lock:
            self._poll_first_seen.pop(key, None)

    def _enforce_poll_deadline(self, key: str, status: str) -> None:
        """Fail a request that has sat in a non-terminal state past the ceiling.

        Records the first time this ``key`` was seen (once), then raises a
        retryable ``TIMEOUT`` ``ProviderError`` once elapsed exceeds
        ``max_poll_seconds``. The deadline entry is deliberately *not* cleared
        on breach: the core poll phase retries ``poll()`` a bounded number of
        times, and each retry must see the deadline still blown so the retry
        budget drains and the step terminates as ``FAILED`` — clearing here
        would reset the clock and re-hang. Cleared instead on a terminal poll
        (see ``poll``), with a FIFO cap as the backstop for abandoned ids.
        """
        if self._max_poll_seconds is None:
            return
        now = time.monotonic()
        with self._poll_first_seen_lock:
            first = self._poll_first_seen.get(key)
            if first is None:
                # FIFO-evict the oldest entry before inserting a new one so a
                # long-lived provider can't grow the map without bound.
                if len(self._poll_first_seen) >= _POLL_FIRST_SEEN_MAX:
                    oldest = next(iter(self._poll_first_seen))
                    self._poll_first_seen.pop(oldest, None)
                self._poll_first_seen[key] = first = now
        elapsed = now - first
        if elapsed >= self._max_poll_seconds:
            raise ProviderError(
                f"GMICloud request {key} stalled in status {status!r} after "
                f"{elapsed:.0f}s (max_poll_seconds={self._max_poll_seconds:.0f}); "
                "upstream never reached a terminal state. Failing the step so it "
                "can be retried.",
                error_code=ProviderErrorCode.TIMEOUT,
            )

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if a GMICloud request is complete (shared across all modalities)."""
        key = str(prediction_id)
        try:
            client = self._get_http_client()
            resp = client.get(f"/requests/{prediction_id}")
            if resp.status_code >= 400:
                inner = unwrap_error_body(resp.text)
                raise ProviderError(
                    f"GMICloud poll failed ({resp.status_code}): {inner}",
                    error_code=map_gmicloud_error(Exception(inner), resp.status_code),
                    retry_after=retry_after_from_response(resp),
                )
            detail = resp.json()
            status = detail.get("status", "")
            if status in _TERMINAL_STATUSES:
                self._clear_poll_deadline(key)
                self._cache_poll_result(prediction_id, detail)
                return True
            # Non-terminal: enforce the connector-side stall ceiling so a wedged
            # upstream job can't poll forever when the caller set a large or
            # unbounded per-step timeout (#262).
            self._enforce_poll_deadline(key, status)
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud poll failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def _fetch_detail(self, prediction_id: Any) -> dict:
        """Retrieve cached poll result, or re-fetch with error checking."""
        detail = self._get_cached_poll_result(prediction_id)
        if detail is not None:
            return detail
        client = self._get_http_client()
        resp = client.get(f"/requests/{prediction_id}")
        if resp.status_code >= 400:
            inner = unwrap_error_body(resp.text)
            raise ProviderError(
                f"GMICloud fetch failed ({resp.status_code}): {inner}",
                error_code=map_gmicloud_error(Exception(inner), resp.status_code),
                retry_after=retry_after_from_response(resp),
            )
        return resp.json()

    # --- Standardization hooks (Phase 3 of provider-standardization-tranche) -

    def preflight_auth(self, *, timeout: float = 5.0) -> None:
        """Cheap auth check — kills the 120s ``submit`` hang on bad credentials.

        ``GET /requests`` with a short timeout returns ``200`` (token valid),
        ``401``/``403`` (token invalid), or a network error. Any non-401/403
        is treated as transient; the user's normal submit timeout governs.

        When the caller injected an ``http_client`` (e.g. tests that supply a
        ``MagicMock``), preflight reuses it so the mock's behaviour governs
        the check — building a fresh ``httpx.Client`` here would bypass the
        injection and dial out for real.

        Skipped automatically when ``GENBLAZE_SKIP_PREFLIGHT`` is set (test
        runners / offline fixtures); see :meth:`BaseProvider.preflight_auth`.
        """
        if not self._api_key and self._http_client is None:
            # No key → nothing to verify; let the existing _get_http_client
            # raise the structured ProviderError on first submit instead.
            return
        try:
            if self._http_client is not None:
                # An http_client is already attached — either injected via
                # __init__ or assigned by a test fixture. Use it so the
                # caller's mock / shared pool / custom transport governs the
                # check; building a fresh httpx.Client here would bypass it.
                resp = self._http_client.get("/requests")
            else:
                # No client yet — build a one-shot with the short preflight
                # timeout so the connector's primary http_timeout (which may
                # be 120s) doesn't apply here.
                with httpx.Client(
                    base_url=self._base_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=timeout,
                ) as client:
                    resp = client.get("/requests")
        except httpx.HTTPError:
            # Transient network errors during preflight should not block the
            # actual submit (which has its own retry / timeout budget).
            return
        if resp.status_code in (401, 403):
            raise ProviderError(
                f"GMICloud rejected GMI_API_KEY (HTTP {resp.status_code}). "
                "Verify the key at https://console.gmicloud.ai/.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )

    def validate_model(self, model_id: str, *, refresh: bool = False) -> ValidationResult:
        """Re-grade probe-confirmed-LIVE to provisional for known-gated slugs.

        ``BaseProvider.validate_model()`` treats a family probe's LIVE
        verdict as authoritative proof a slug is callable. For GMICloud
        that's only true some of the time — see ``_entitlement_gated_slugs``
        above for why the probe can't see entitlement failures. This
        override leaves the base behavior untouched for every other slug
        (the common case, and what the existing probe test suite pins) and
        only re-grades the specific slugs this connector knows are gated,
        so ``result.outcome`` genuinely distinguishes "known slug" from
        "confirmed callable with this key" instead of collapsing both into
        ``OK_AUTHORITATIVE``.
        """
        result = super().validate_model(model_id, refresh=refresh)
        if (
            result.outcome is ValidationOutcome.OK_AUTHORITATIVE
            and result.source is ValidationSource.PROBE
            and self._is_entitlement_gated(model_id)
        ):
            return replace(
                result,
                outcome=ValidationOutcome.OK_PROVISIONAL,
                detail=(
                    f"known slug ({model_id!r} exists in GMICloud's catalog per "
                    "the request-queue probe) but NOT confirmed callable with "
                    "this API key — this slug is known to require additional "
                    "GMICloud account entitlement, and the probe can't detect "
                    "that (payload shape is validated before entitlement, so "
                    "the empty-payload probe never reaches the same check a "
                    "real submit hits). A real submit may still 404 with 'you "
                    "do not have access'; verify catalog access at "
                    "https://console.gmicloud.ai/ before running. See #193."
                ),
            )
        return result

    def _is_entitlement_gated(self, model_id: str) -> bool:
        """True if ``model_id`` (raw or wire-canonical) is a known-gated slug."""
        if model_id in self._entitlement_gated_slugs:
            return True
        return self._models.resolve_canonical(model_id) in self._entitlement_gated_slugs

    # ``probe_model()`` is intentionally not overridden here. As of
    # genblaze-core 0.3.0 the legacy ``probe_model`` adapter on
    # ``BaseProvider`` delegates to ``validate_model(refresh=True)``
    # which routes through ``_invoke_family_probe`` →
    # ``empty_payload_request_probe``. That path handles 404/400/2xx
    # exactly like the previous override (including the cancel-on-2xx
    # phantom-job cleanup), shares the in-flight + LRU probe cache, and
    # produces a single source of truth for slug-validity questions.
    # Removed: the previous override that duplicated probe logic and
    # could disagree with ``validate_model`` for the same slug (red-team
    # finding #11 on PR #5).
