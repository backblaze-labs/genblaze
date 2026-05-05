"""Shared base for all GMICloud media providers (video, image, audio).

Owns auth, HTTP client lifecycle, and the common poll() implementation
since all modalities use the same async request queue API.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers import (
    DiscoverySupport,
    LiveProbeResult,
)
from genblaze_core.providers.base import BaseProvider, SubmitResult
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.retry import RetryPolicy, retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_gmicloud_error

_DEFAULT_BASE_URL = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"

_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})

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

    def _invoke_family_probe(self, probe: Any, model_id: str) -> LiveProbeResult:
        """Forward the family probe with this provider's ``httpx.Client``."""
        return probe(model_id, http=self._get_http_client())

    def __init__(
        self,
        api_key: str | None = None,
        *,
        poll_interval: float = 5.0,
        http_timeout: float = 120.0,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        # Forward models= to BaseProvider so the documented per-instance
        # registry override actually takes effect (closes feedback P0-03).
        super().__init__(models=models, retry_policy=retry_policy)
        self.poll_interval = poll_interval
        self._api_key: str | None = api_key or os.environ.get("GMI_API_KEY")
        self._http_timeout = http_timeout
        self._base_url: str = base_url or os.environ.get("GMI_BASE_URL") or _DEFAULT_BASE_URL
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

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if a GMICloud request is complete (shared across all modalities)."""
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
            if detail.get("status", "") in _TERMINAL_STATUSES:
                self._cache_poll_result(prediction_id, detail)
                return True
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
