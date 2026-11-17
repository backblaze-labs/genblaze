"""Atlas Cloud image and video generation provider.

Atlas Cloud exposes one asynchronous prediction contract across its media
catalog: submit an image or video request, then poll the returned prediction.
The connector deliberately never retries submission. A transport failure after
a POST is ambiguous, and automatically submitting again could create a second
billable generation.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    ModelRegistry,
    ProviderCapabilities,
    RetryPolicy,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_atlascloud_error

_DEFAULT_BASE_URL = "https://api.atlascloud.ai"
_TERMINAL_STATUSES = frozenset({"completed", "succeeded", "failed", "timeout", "canceled"})
_FAILED_STATUSES = frozenset({"failed", "timeout", "canceled"})
_URL_PARAM_KEYS = frozenset(
    {"image", "image_url", "images", "video", "video_url", "audio", "audio_url"}
)


def _unwrap_response(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Atlas Cloud returned a non-object response")
    if payload.get("code") not in (None, 0, 200):
        message = payload.get("message") or payload.get("msg") or "unknown API error"
        raise ValueError(f"Atlas Cloud API error: {message}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ValueError("Atlas Cloud response data is not an object")
    return data


def _validate_url_params(params: dict[str, Any]) -> None:
    for key in _URL_PARAM_KEYS:
        value = params.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item:
                validate_chain_input_url(item)


class AtlasCloudProvider(BaseProvider):
    """Opt-in Atlas Cloud adapter for image and video generation.

    Args:
        api_key: Atlas Cloud API key. Defaults to ``ATLASCLOUD_API_KEY``.
        base_url: API origin. Defaults to ``https://api.atlascloud.ai``.
        poll_interval: Initial seconds between prediction polls.
        poll_get_retries: Bounded retries for transient prediction GET failures.
        http_timeout: Per-request HTTP timeout in seconds.
        http_client: Optional injected client, primarily for tests.
        models: Optional custom model registry.
        retry_policy: Optional core retry policy. The default has one attempt so
            the core never repeats a potentially billable POST.
    """

    name = "atlascloud"
    discovery_support = DiscoverySupport.NONE

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        poll_interval: float = 3.0,
        poll_get_retries: int = 2,
        http_timeout: float = 120.0,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ) -> None:
        if poll_get_retries < 0:
            raise ValueError("poll_get_retries must be >= 0")
        super().__init__(
            models=models,
            retry_policy=retry_policy or RetryPolicy(max_attempts=1),
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self.poll_get_retries = poll_get_retries
        self._api_key = api_key or os.getenv("ATLASCLOUD_API_KEY")
        self._base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(timeout=http_timeout)

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO],
            supported_inputs=["text", "image", "video", "audio"],
            accepts_chain_input=False,
            models=self._models.known(),
            output_formats=["image/jpeg", "image/png", "video/mp4"],
        )

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "Atlas Cloud API key is required. Set ATLASCLOUD_API_KEY or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _endpoint_for(self, step: Step) -> str:
        if step.modality == Modality.IMAGE:
            return "/api/v1/model/generateImage"
        if step.modality == Modality.VIDEO:
            return "/api/v1/model/generateVideo"
        raise ProviderError(
            "Atlas Cloud generation requires modality=Modality.IMAGE or Modality.VIDEO",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit exactly one generation request; this method never retries."""
        params = dict(step.params)
        _validate_url_params(params)
        payload = {**params, "model": step.model, "prompt": step.prompt or ""}
        try:
            response = self._client.post(
                f"{self._base_url}{self._endpoint_for(step)}",
                headers=self._headers(),
                json=payload,
            )
            data = _unwrap_response(response)
            prediction_id = data.get("id") or data.get("request_id")
            if not prediction_id:
                raise ValueError("Atlas Cloud submit response did not include a prediction id")
            return str(prediction_id)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Atlas Cloud submit failed: {exc}",
                error_code=map_atlascloud_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def _get_prediction(self, prediction_id: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.poll_get_retries + 1):
            try:
                response = self._client.get(
                    f"{self._base_url}/api/v1/model/prediction/{prediction_id}",
                    headers=self._headers(),
                )
                return _unwrap_response(response)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == self.poll_get_retries:
                    break
                time.sleep(min(0.25 * (2**attempt), 1.0))
        assert last_error is not None
        raise last_error

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        try:
            data = self._get_prediction(prediction_id)
            status = str(data.get("status", "")).lower()
            if status in _TERMINAL_STATUSES:
                self._cache_poll_result(prediction_id, data)
                return True
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Atlas Cloud poll failed: {exc}",
                error_code=map_atlascloud_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        try:
            data = self._get_cached_poll_result(prediction_id)
            if data is None:
                data = self._get_prediction(prediction_id)
            status = str(data.get("status", "")).lower()
            if status in _FAILED_STATUSES:
                message = data.get("error") or data.get("message") or status
                raise ProviderError(
                    f"Atlas Cloud generation failed: {message}",
                    error_code=ProviderErrorCode.MODEL_ERROR,
                )
            outputs = data.get("outputs") or data.get("output") or []
            if isinstance(outputs, str):
                outputs = [outputs]
            if not isinstance(outputs, list) or not outputs:
                raise ProviderError(
                    "Atlas Cloud completed without output URLs",
                    error_code=ProviderErrorCode.MODEL_ERROR,
                )
            media_type = "video/mp4" if step.modality == Modality.VIDEO else "image/jpeg"
            for url in outputs:
                if not isinstance(url, str):
                    continue
                validate_asset_url(url)
                step.assets.append(Asset(url=url, media_type=media_type))
            step.provider_payload = {
                "atlascloud": {
                    "prediction_id": str(prediction_id),
                    "status": status,
                }
            }
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Atlas Cloud output fetch failed: {exc}",
                error_code=map_atlascloud_error(exc),
            ) from exc
