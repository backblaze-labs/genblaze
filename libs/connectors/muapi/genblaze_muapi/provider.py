"""MuAPI image, video, and audio generation provider.

MuAPI exposes one asynchronous contract for its media catalog:

* ``GET /api/v1/models`` lists enabled models, categories, and input metadata.
* ``POST /api/v1/{model}`` accepts the model-specific JSON payload and returns
  a ``request_id``.
* ``GET /api/v1/predictions/{request_id}/result`` returns processing state and,
  when complete, hosted output URLs.

The connector keeps model names dynamic. MuAPI's catalog is deliberately not
copied into this package: native discovery makes newly enabled media models
usable without a package release while the registry's generic input surface
preserves model-specific fields.
"""

from __future__ import annotations

import mimetypes
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    route_by_media_type,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.providers.discovery import DEFAULT_TTL_SECONDS, DiscoveryResult, _DiscoveryCache
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import describe_muapi_error, map_muapi_error, map_muapi_result_error

_DEFAULT_BASE_URL = "https://api.muapi.ai/api/v1"
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
_TERMINAL_STATUSES = frozenset({"completed", "succeeded", "failed", "cancelled", "canceled"})
_MEDIA_CATEGORIES = ("image", "video", "audio")
_URL_PARAM_NAMES = frozenset(
    {
        "audio",
        "image",
        "images",
        "mask",
        "mask_image",
        "reference_image",
        "reference_images",
        "reference_video",
        "reference_videos",
        "video",
        "videos",
    }
)
_URL_PARAM_SUFFIXES = ("_url", "_urls")
_DEFAULT_MEDIA_TYPES = {
    Modality.IMAGE: "image/png",
    Modality.VIDEO: "video/mp4",
    Modality.AUDIO: "audio/mpeg",
}
_INPUT_MAPPING = route_by_media_type(
    {"image": "image_url", "video": "video_url", "audio": "audio_url"}
)
_MUAPI_FAMILY = ModelFamily(
    name="muapi-media",
    pattern=_MODEL_ID_RE,
    spec_template=ModelSpec(model_id="*", input_mapping=_INPUT_MAPPING),
    description="MuAPI's dynamically discovered image, video, and audio models.",
    example_slugs=("flux-schnell", "seedance-2.5-text-to-video", "wan2.2-image-to-video"),
)
_MUAPI_FALLBACK = ModelSpec(model_id="*", input_mapping=_INPUT_MAPPING)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MuAPI returned a non-object JSON response")
    return payload


def _is_url_param(key: Any) -> bool:
    name = str(key).lower()
    return name in _URL_PARAM_NAMES or name.endswith(_URL_PARAM_SUFFIXES)


def _validate_input_url(url: str) -> None:
    """Validate one URL that MuAPI's worker will fetch."""
    if url[:5].lower() == "data:":
        return
    validate_chain_input_url(url)
    if urlparse(url).scheme == "file":
        raise ProviderError(
            "MuAPI cannot read local file inputs; upload the asset and pass an https:// URL",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _validate_url_params(value: Any, *, url_slot: bool = False) -> None:
    """Walk nested model parameters and validate URL-bearing values."""
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_url_params(item, url_slot=_is_url_param(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_url_params(item, url_slot=url_slot)
    elif url_slot and isinstance(value, str) and value:
        _validate_input_url(value)


def _status_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten MuAPI's occasional ``detail``-wrapped result envelope."""
    detail = data.get("detail")
    if not isinstance(detail, dict):
        return data
    merged = dict(detail)
    merged.update({key: value for key, value in data.items() if key != "detail"})
    return merged


def _status(data: dict[str, Any]) -> str:
    value = _status_payload(data).get("status")
    return str(value or "").lower()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _cost(data: dict[str, Any]) -> float | None:
    value = _status_payload(data).get("cost")
    if isinstance(value, dict):
        value = value.get("amount_usd", value.get("usd", value.get("amount")))
    return _number(value)


def _catalog_row_is_enabled(row: dict[str, Any]) -> bool:
    """Accept both the public API's snake_case flag and source-data casing."""
    value = row.get("is_enabled", row.get("isEnabled", True))
    return bool(value)


def _output_items(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return output URLs and their optional metadata from a result envelope."""
    outputs = _status_payload(data).get("outputs")
    if isinstance(outputs, dict):
        outputs = [outputs]
    if isinstance(outputs, str):
        outputs = [outputs]
    if not isinstance(outputs, list):
        return []

    items: list[tuple[str, dict[str, Any]]] = []
    for item in outputs:
        if isinstance(item, str):
            items.append((item, {}))
            continue
        if not isinstance(item, dict):
            continue
        for key in ("url", "image_url", "video_url", "audio_url", "output_url"):
            url = item.get(key)
            if isinstance(url, str):
                items.append((url, item))
                break
    return items


def _media_type(url: str, metadata: dict[str, Any], modality: Modality) -> str:
    content_type = metadata.get("content_type") or metadata.get("media_type")
    if isinstance(content_type, str) and "/" in content_type:
        return content_type.split(";", 1)[0].strip().lower()
    kind = str(metadata.get("type") or metadata.get("media_type") or "").lower()
    if kind in {modality.value for modality in _DEFAULT_MEDIA_TYPES}:
        return _DEFAULT_MEDIA_TYPES[Modality(kind)]
    return mimetypes.guess_type(urlparse(url).path)[0] or _DEFAULT_MEDIA_TYPES.get(
        modality, "application/octet-stream"
    )


class MuAPIProvider(BaseProvider):
    """MuAPI's dynamically discovered asynchronous media provider.

    Any enabled MuAPI image, video, or audio model can be passed as ``model``;
    model-specific fields belong in ``params`` and are forwarded unchanged.
    """

    name = "muapi"
    discovery_support = DiscoverySupport.NATIVE

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(provider_families=(_MUAPI_FAMILY,), fallback=_MUAPI_FALLBACK)

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        poll_interval: float = 2.0,
        http_timeout: float = 60.0,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ) -> None:
        parsed = urlparse(base_url.rstrip("/"))
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path.rstrip("/") != "/api/v1"
            or any(c in base_url for c in "?#;")
            or parsed.username is not None
            or parsed.password is not None
            or port == -1
            or port == 0
        ):
            raise ValueError(
                "base_url must be a bare https:// origin ending in /api/v1 "
                f"(no query, fragment, or credentials), got {base_url!r}"
            )
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self._api_key = api_key or os.getenv("MUAPI_API_KEY")
        port_suffix = f":{port}" if port not in (None, 443) else ""
        self._base_url = f"https://{parsed.hostname}{port_suffix}/api/v1"
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=http_timeout)
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_models,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    def close(self) -> None:
        """Release the internal HTTP client's connection pool."""
        if self._owns_client:
            self._client.close()

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO, Modality.AUDIO],
            supported_inputs=["text", "image", "video", "audio"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["image/jpeg", "image/png", "video/mp4", "audio/mpeg", "audio/wav"],
        )

    def _headers(self, *, auth_required: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        elif auth_required:
            raise ProviderError(
                "MuAPI API key is required. Set MUAPI_API_KEY or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _get_result(self, request_id: str, *, auth_required: bool = True) -> dict[str, Any]:
        return _json_object(
            self._client.get(
                self._url(f"predictions/{request_id}/result"),
                headers=self._headers(auth_required=auth_required),
            )
        )

    @staticmethod
    def _wrap(phase: str, exc: Exception) -> ProviderError:
        detail = (
            describe_muapi_error(exc.response) if isinstance(exc, httpx.HTTPStatusError) else exc
        )
        return ProviderError(
            f"MuAPI {phase} failed: {detail}",
            error_code=map_muapi_error(exc),
            retry_after=retry_after_from_response(exc),
        )

    # --- catalog discovery -------------------------------------------------

    def _fetch_models(self) -> DiscoveryResult:
        """Fetch and filter MuAPI's authoritative media catalog."""
        source_url = self._url("models")
        try:
            payload = _json_object(
                self._client.get(source_url, headers=self._headers(auth_required=False))
            )
            rows = payload.get("models")
            if not isinstance(rows, list):
                return DiscoveryResult.failed(
                    "MuAPI models response omitted a models list", source_url=source_url
                )
            slugs = {
                row["name"]
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and _MODEL_ID_RE.fullmatch(row["name"])
                and _catalog_row_is_enabled(row)
                and not row.get("is_coming_soon")
                and str(row.get("domain", "ai")).lower() == "ai"
                and any(
                    token in str(row.get("category", "")).lower() for token in _MEDIA_CATEGORIES
                )
            }
            return DiscoveryResult.ok(slugs, source_url=source_url)
        except Exception as exc:
            return DiscoveryResult.failed(
                f"MuAPI models request failed: {exc}", source_url=source_url
            )

    def discover_models(
        self,
        *,
        max_age_seconds: float | None = ...,  # type: ignore[assignment]
    ) -> DiscoveryResult:
        """Return the cached live media catalog, fetching it when stale."""
        cache = self._models._discovery_cache
        assert cache is not None
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            return cache.get()
        return cache.get(max_age_seconds=max_age_seconds)

    # --- lifecycle ---------------------------------------------------------

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit one MuAPI job and return its safe request id."""
        if not _MODEL_ID_RE.fullmatch(step.model):
            raise ProviderError(
                f"Invalid MuAPI model id {step.model!r}; expected a catalog slug",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        try:
            payload = self.prepare_payload(step)
            _validate_url_params(payload)
            response = self._client.post(
                self._url(step.model),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
            )
            data = _json_object(response)
        except ProviderError:
            raise
        except Exception as exc:
            # Do not hide an ambiguous POST behind a provider-side retry.
            raise self._wrap("submit", exc) from exc
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise ProviderError(
                f"MuAPI submit response did not include a usable request_id: {request_id!r}",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        return request_id

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        request_id = str(prediction_id)
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ProviderError(
                f"Invalid MuAPI prediction id {request_id!r}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        try:
            data = self._get_result(request_id)
        except Exception as exc:
            raise self._wrap("poll", exc) from exc
        if _status(data) in _TERMINAL_STATUSES:
            self._cache_poll_result(prediction_id, data)
            return True
        return False

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        request_id = str(prediction_id)
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ProviderError(
                f"Invalid MuAPI prediction id {request_id!r}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        data = self._get_cached_poll_result(prediction_id)
        if data is None:
            try:
                data = self._get_result(request_id)
            except Exception as exc:
                raise self._wrap("result fetch", exc) from exc
        normalized = _status_payload(data)
        if _status(normalized) in {"failed", "cancelled", "canceled"}:
            error = normalized.get("error") or normalized.get("message") or "generation failed"
            if isinstance(error, dict):
                error = error.get("message") or error.get("detail") or str(error)
            message = str(error)[:500]
            raise ProviderError(
                f"MuAPI request {request_id} failed: {message}",
                error_code=map_muapi_result_error(message),
            )

        assets: list[Asset] = []
        for url, metadata in _output_items(normalized):
            try:
                validate_asset_url(url)
            except ProviderError as exc:
                raise ProviderError(
                    f"MuAPI request {request_id} returned an unusable output URL: {url[:80]!r}",
                    error_code=ProviderErrorCode.MODEL_ERROR,
                ) from exc
            media_type = _media_type(url, metadata, step.modality)
            assets.append(
                Asset(
                    url=url,
                    media_type=media_type,
                    width=metadata.get("width")
                    if isinstance(metadata.get("width"), int)
                    else None,
                    height=metadata.get("height")
                    if isinstance(metadata.get("height"), int)
                    else None,
                    duration=_number(metadata.get("duration")),
                    size_bytes=metadata.get("size_bytes")
                    if isinstance(metadata.get("size_bytes"), int) and metadata["size_bytes"] >= 0
                    else None,
                    audio=AudioMetadata() if media_type.startswith("audio/") else None,
                )
            )
        if not assets:
            raise ProviderError(
                f"MuAPI request {request_id} completed without media outputs",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )

        step.assets.extend(assets)
        step.provider_payload = {"muapi": {"request_id": request_id}}
        reported_cost = _cost(normalized)
        if reported_cost is not None:
            step.cost_usd = reported_cost
            step.provider_payload["muapi"]["cost_usd"] = reported_cost
        return step
