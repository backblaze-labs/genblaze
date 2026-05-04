"""ReplicateProvider — adapter for the Replicate API.

Replicate hosts thousands of models. Slugs are free-form ``owner/name``
strings — any of them is a valid input. The connector ships:

* ``DiscoverySupport.NATIVE`` — Replicate's API can authoritatively answer
  "is this slug live?" via ``GET /v1/models/{owner}/{name}``.
* An empty registry with a permissive fallback. There are no per-slug
  default specs because Replicate's surface is too large to enumerate.
* A custom ``validate_model()`` that does a per-slug ``models.get()``
  call (cached per-process, TTL-bounded) rather than enumerating the
  full catalog. This is what NATIVE means for Replicate in practice.
* A custom ``discover_models()`` that returns the *first page* of the
  catalog as a documentation hint — enough to populate ``known()`` for
  IDE autocomplete and conformance testing without paying the cost of
  enumerating ~10k models on every cold start.

**Pricing**: Replicate was previously hardcoded to
``predict_time × 0.000225`` USD on the fallback spec. As of
``genblaze-core 0.3.0`` the SDK no longer ships pricing — register the
recipe yourself if you want compute-time cost tracking. See
``docs/reference/pricing-recipes.md`` for the canonical Replicate recipe.

Docs: https://replicate.com/docs/reference/http
"""

from __future__ import annotations

import logging
import mimetypes
import threading
import time
from typing import Any
from urllib.parse import urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoveryResult,
    DiscoveryStatus,
    DiscoverySupport,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    ValidationOutcome,
    ValidationResult,
    ValidationSource,
    route_by_media_type,
    validate_asset_url,
)
from genblaze_core.providers.discovery import DEFAULT_TTL_SECONDS, _DiscoveryCache
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_replicate_error

logger = logging.getLogger("genblaze.replicate")

# Cap for the first-page discovery snapshot. Replicate's /v1/models
# returns ~25 models per page; one page is enough to seed known() for
# IDE/conformance purposes without enumerating the full catalog.
_DISCOVERY_PAGE_LIMIT: int = 25

# Replicate has no enumerable per-slug defaults. The fallback applies to
# every model id with route_by_media_type wiring chain inputs to the
# common ``image``/``video``/``audio`` keys most Replicate models accept.
_FALLBACK_SPEC = ModelSpec(
    model_id="*",
    input_mapping=route_by_media_type({"image": "image", "video": "video", "audio": "audio"}),
)


class ReplicateProvider(BaseProvider):
    """Provider adapter for Replicate (replicate.com)."""

    name = "replicate"
    discovery_support = DiscoverySupport.NATIVE
    """Replicate exposes ``GET /v1/models/{owner}/{name}`` for authoritative
    per-slug existence checks. The catalog itself is too large to enumerate
    (~10k+ public models), so this provider validates per-slug rather than
    snapshotting the full catalog."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        # No enumerable defaults — every model uses the fallback spec.
        # Discovery cache is wired per-instance in __init__ so each
        # provider sees its own credentials' view of the catalog.
        return ModelRegistry(defaults={}, fallback=_FALLBACK_SPEC)

    def get_capabilities(self) -> ProviderCapabilities:
        """Replicate: multi-modal generation depending on selected model."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO, Modality.AUDIO],
            supported_inputs=["text", "image", "video", "audio"],
            accepts_chain_input=True,
        )

    def __init__(
        self,
        api_token: str | None = None,
        poll_interval: float = 1.0,
        http_timeout: float = 30.0,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(models=models, retry_policy=retry_policy)
        self.poll_interval = poll_interval
        self._client: Any = None
        self._api_token = api_token
        self._http_timeout = http_timeout
        # Per-slug authoritative-validation cache. Stored as
        # ``slug → (fetched_monotonic, ValidationResult)`` so we honor a TTL
        # without re-issuing per-slug GETs on every Pipeline.run(). RLock
        # keeps concurrent ``validate_model`` callers from racing on the
        # cache writes; reads are fast-path lockless once populated.
        self._validation_cache: dict[str, tuple[float, ValidationResult]] = {}
        self._validation_lock = threading.RLock()
        self._validation_ttl: float = DEFAULT_TTL_SECONDS
        # Wire a discovery cache for the first-page snapshot. The fetcher
        # closes over ``self`` so it picks up the (possibly-late-bound)
        # ``self._client``.
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_first_page,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    # --- catalog discovery & validation ------------------------------------

    def _fetch_first_page(self) -> DiscoveryResult:
        """Fetcher backing ``discover_models`` — first page of /v1/models.

        Replicate's full catalog is too large to enumerate; one page is
        enough to seed ``known()`` for IDE / conformance purposes. The
        authoritative per-slug check lives in ``validate_model``.
        """
        try:
            client = self._get_client()
            page = client.models.list()
            slugs: set[str] = set()
            results = getattr(page, "results", None) or list(page)
            for model in results[:_DISCOVERY_PAGE_LIMIT]:
                owner = getattr(model, "owner", None)
                name = getattr(model, "name", None)
                if owner and name:
                    slugs.add(f"{owner}/{name}")
            return DiscoveryResult.ok(slugs, source_url="https://api.replicate.com/v1/models")
        except Exception as exc:
            logger.warning("Replicate discover_models first-page fetch failed: %s", exc)
            return DiscoveryResult.failed(
                f"first-page enumeration failed: {exc}",
                source_url="https://api.replicate.com/v1/models",
            )

    def discover_models(
        self,
        *,
        max_age_seconds: float | None = ...,  # type: ignore[assignment]
    ) -> DiscoveryResult:
        """Return the first-page snapshot of Replicate's catalog.

        This is intentionally not exhaustive — Replicate hosts thousands
        of models, and ``known()`` is documentation-grade, not a
        contract. For authoritative existence checks, use
        ``validate_model(slug)``, which does a per-slug ``models.get()``.
        """
        cache = self._models._discovery_cache
        assert cache is not None  # wired in __init__
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            return cache.get()
        return cache.get(max_age_seconds=max_age_seconds)

    def validate_model(self, model_id: str, *, refresh: bool = False) -> ValidationResult:
        """Authoritative per-slug existence check.

        Replicate's per-model GET is cheap (one round-trip, no token spend)
        and returns 404 for missing slugs. We cache the result per-slug
        with a TTL so successive ``Pipeline.run()`` invocations don't
        re-fetch. ``refresh=True`` evicts the cache entry first.

        Falls back to the base ``validate_model`` flow when the slug is
        user-registered or matches a registered family — those layers are
        already authoritative without a network call.
        """
        # Defer to the base class first — user-registered specs and
        # family matches with cached discovery hits don't need a probe.
        base_result = self._models.validate(model_id, discovery_support=self.discovery_support)
        if (
            base_result.outcome is ValidationOutcome.OK_AUTHORITATIVE
            and base_result.source is not ValidationSource.DISCOVERY
        ):
            # USER or PROBE-source authoritative answer — no network needed.
            return base_result

        if refresh:
            with self._validation_lock:
                self._validation_cache.pop(model_id, None)
        else:
            cached = self._lookup_validation_cache(model_id)
            if cached is not None:
                return cached

        result = self._authoritative_lookup(model_id)
        with self._validation_lock:
            self._validation_cache[model_id] = (time.monotonic(), result)
        return result

    def _lookup_validation_cache(self, model_id: str) -> ValidationResult | None:
        with self._validation_lock:
            entry = self._validation_cache.get(model_id)
            if entry is None:
                return None
            fetched_at, result = entry
            if (time.monotonic() - fetched_at) > self._validation_ttl:
                # Expired — evict and force a fresh lookup.
                del self._validation_cache[model_id]
                return None
            return result

    def _authoritative_lookup(self, model_id: str) -> ValidationResult:
        """Single-slug GET against /v1/models/{owner}/{name}.

        Translates the Replicate SDK's responses into ``ValidationResult``:

        * Success → ``OK_AUTHORITATIVE`` (source PROBE — same shape a
          PARTIAL provider's family.probe would produce).
        * 404 / NotFound → ``NOT_FOUND``.
        * Other errors → ``UNKNOWN_PERMISSIVE`` (we couldn't verify; let
          the upstream call surface the real error if there is one).
        """
        try:
            client = self._get_client()
            client.models.get(model_id)
        except Exception as exc:
            err_code = map_replicate_error(exc)
            if err_code is ProviderErrorCode.MODEL_ERROR:
                return ValidationResult.not_found(
                    ValidationSource.PROBE,
                    detail=f"upstream returned not-found: {exc}",
                )
            logger.debug(
                "Replicate validate_model probe inconclusive for %s: %s",
                model_id,
                exc,
            )
            return ValidationResult.unknown_permissive(detail=f"probe inconclusive: {exc}")
        return ValidationResult.ok_authoritative(
            ValidationSource.PROBE,
            detail="confirmed via /v1/models/{owner}/{name}",
        )

    def _get_client(self):
        if self._client is None:
            try:
                import httpx
                import replicate

                timeout = httpx.Timeout(self._http_timeout, connect=10.0)
                if self._api_token:
                    self._client = replicate.Client(
                        api_token=self._api_token,  # noqa: S106
                        timeout=timeout,
                    )
                else:
                    self._client = replicate.Client(timeout=timeout)
            except ImportError as exc:
                raise ProviderError(
                    "replicate package not installed. Run: pip install replicate"
                ) from exc
        return self._client

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        client = self._get_client()
        try:
            input_params = self.prepare_payload(step)
            prediction = client.predictions.create(
                model=step.model,
                input=input_params,
            )
            return prediction.id
        except Exception as exc:
            raise ProviderError(
                f"Replicate submit failed: {exc}",
                error_code=map_replicate_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        client = self._get_client()
        try:
            prediction = client.predictions.get(prediction_id)
            if prediction.status in ("succeeded", "failed", "canceled"):
                self._cache_poll_result(prediction_id, prediction)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"Replicate poll failed: {exc}",
                error_code=map_replicate_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        client = self._get_client()
        try:
            prediction = self._get_cached_poll_result(prediction_id)
            if prediction is None:
                prediction = client.predictions.get(prediction_id)

            # Capture predict_time so registry pricing can read it.
            metrics = getattr(prediction, "metrics", None)
            predict_time = getattr(metrics, "predict_time", None) if metrics else None

            step.provider_payload = {
                "replicate": {
                    "prediction_id": prediction.id,
                    "model": prediction.model if hasattr(prediction, "model") else None,
                    "version": prediction.version if hasattr(prediction, "version") else None,
                    "status": prediction.status,
                    "created_at": str(prediction.created_at)
                    if hasattr(prediction, "created_at")
                    else None,
                    "predict_time": predict_time,
                }
            }

            if prediction.status == "failed":
                error_msg = prediction.error or "Unknown error"
                raise ProviderError(
                    error_msg,
                    error_code=map_replicate_error(error_msg),
                )

            if prediction.status == "canceled":
                raise ProviderError(
                    "Prediction was canceled",
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Replicate output shapes vary by model: str (single URL), list[str]
            # (multi-asset), dict[str, str | list] (e.g. {"video": url,
            # "subtitles": url} from text-to-video models with side-channels),
            # or None (no output). Normalize to list[str].
            raw_output = prediction.output
            urls: list[str]
            if raw_output is None:
                urls = []
            elif isinstance(raw_output, str):
                urls = [raw_output]
            elif isinstance(raw_output, list):
                # Nested lists happen on batch-output models; flatten one level.
                urls = []
                for item in raw_output:
                    if isinstance(item, str):
                        urls.append(item)
                    elif isinstance(item, list):
                        urls.extend(str(u) for u in item if isinstance(u, (str, bytes)))
            elif isinstance(raw_output, dict):
                # Multi-channel outputs: keep only URL-shaped string values.
                urls = [str(v) for v in raw_output.values() if isinstance(v, str)]
            else:
                raise ProviderError(
                    f"Unexpected Replicate output shape "
                    f"({type(raw_output).__name__}): {raw_output!r}",
                    error_code=ProviderErrorCode.SERVER_ERROR,
                )

            for url_str in urls:
                validate_asset_url(url_str)
                path = urlparse(url_str).path
                mime, _ = mimetypes.guess_type(path)
                if mime is None:
                    mime = f"{step.modality.value}/octet-stream"
                step.assets.append(Asset(url=url_str, media_type=mime))

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Replicate fetch_output failed: {exc}",
                error_code=map_replicate_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
