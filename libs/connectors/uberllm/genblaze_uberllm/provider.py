"""UberLLM image and video generation provider.

UberLLM serves image and video models from many vendors behind one API key.
Images come back in the response to ``POST /v1/images/generations`` (or
``/v1/images/edits`` when a source picture is given). Videos are jobs: submit
to ``POST /v1/video/generations``, then poll ``GET /v1/video/generations/{id}``.
Both report the billed cost in ``usage.cost_usd``.

Outputs on the API host (``/v1/media/artifacts/...``) need the API key to
download, so the connector saves them as local ``file://`` assets. The
connector never retries a submission: a transport failure after a POST is
ambiguous and submitting again could create a second billable generation.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from genblaze_core._utils import local_file_url
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

from ._errors import map_uberllm_error

_DEFAULT_BASE_URL = "https://api.uberllm.dev"
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "expired"})
_FAILED_STATUSES = frozenset({"failed", "canceled", "expired"})
# Params that carry a source picture; their presence routes an image step to /images/edits.
_SOURCE_IMAGE_KEYS = frozenset({"image", "image_url", "input_images", "images"})
_URL_PARAM_KEYS = _SOURCE_IMAGE_KEYS | {"end_image_url"}
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}


def _unwrap_response(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        try:
            error = response.json().get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
        except ValueError:
            message = None
        raise httpx.HTTPStatusError(
            f"HTTP {response.status_code}: {message or response.reason_phrase}",
            request=response.request,
            response=response,
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("UberLLM returned a non-object response")
    return payload


def _validate_url_params(params: dict[str, Any]) -> None:
    for key in _URL_PARAM_KEYS:
        value = params.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item = item.get("url") or item.get("image_url")
            # data: URIs, plain base64 and file_ ids are sent inline, not fetched.
            if isinstance(item, str) and "://" in item:
                validate_chain_input_url(item)


def _sniff_image_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class UberLLMProvider(BaseProvider):
    """Opt-in UberLLM adapter for image and video generation.

    Args:
        api_key: UberLLM API key. Defaults to ``UBERLLM_API_KEY``.
        base_url: API origin. Defaults to ``https://api.uberllm.dev``.
        output_dir: Directory for downloaded outputs (default system temp).
        poll_interval: Initial seconds between video job polls.
        poll_get_retries: Bounded retries for transient job GET failures.
        http_timeout: Per-request HTTP timeout in seconds.
        http_client: Optional injected client, primarily for tests.
        models: Optional custom model registry.
        retry_policy: Optional core retry policy. The default has one attempt so
            the core never repeats a potentially billable POST.
    """

    name = "uberllm"
    discovery_support = DiscoverySupport.NONE

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        output_dir: str | Path | None = None,
        poll_interval: float = 5.0,
        poll_get_retries: int = 2,
        http_timeout: float = 300.0,
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
        self._api_key = api_key or os.getenv("UBERLLM_API_KEY")
        self._base_url = base_url.rstrip("/")
        self._output_dir = Path(output_dir) if output_dir else None
        self._client = http_client or httpx.Client(timeout=http_timeout)
        self._image_results: dict[str, dict[str, Any]] = {}

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=False,
            models=self._models.known(),
            output_formats=["image/png", "image/jpeg", "image/webp", "video/mp4"],
        )

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "UberLLM API key is required. Set UBERLLM_API_KEY or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit exactly one generation request; this method never retries."""
        if step.modality not in (Modality.IMAGE, Modality.VIDEO):
            raise ProviderError(
                "UberLLM generation requires modality=Modality.IMAGE or Modality.VIDEO",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        params = dict(step.params)
        _validate_url_params(params)
        payload = {**params, "model": step.model, "prompt": step.prompt or ""}
        try:
            if step.modality == Modality.VIDEO:
                response = self._client.post(
                    f"{self._base_url}/v1/video/generations",
                    headers=self._headers(),
                    json=payload,
                )
                job_id = _unwrap_response(response).get("id")
                if not job_id:
                    raise ValueError("UberLLM video response did not include a job id")
                return str(job_id)
            route = "edits" if _SOURCE_IMAGE_KEYS & params.keys() else "generations"
            payload.setdefault("response_format", "b64_json")
            response = self._client.post(
                f"{self._base_url}/v1/images/{route}",
                headers=self._headers(),
                json=payload,
            )
            data = _unwrap_response(response)
            # Images come back in the response, so the "prediction" is already done.
            result_id = f"image-{uuid.uuid4()}"
            self._image_results[result_id] = data
            return result_id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"UberLLM submit failed: {exc}",
                error_code=map_uberllm_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def _get_job(self, job_id: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.poll_get_retries + 1):
            try:
                response = self._client.get(
                    f"{self._base_url}/v1/video/generations/{job_id}",
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
        if prediction_id in self._image_results:
            return True
        try:
            data = self._get_job(prediction_id)
            status = str(data.get("status", "")).lower()
            if status in _TERMINAL_STATUSES:
                self._cache_poll_result(prediction_id, data)
                return True
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"UberLLM poll failed: {exc}",
                error_code=map_uberllm_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def _save(self, content: bytes, step: Step, index: int, media_type: str) -> Asset:
        ext = _EXTENSIONS.get(media_type, "")
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}_{index}{ext}"
        else:
            fd, tmp = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            out_path = Path(tmp)
        out_path.write_bytes(content)
        return Asset(
            url=local_file_url(out_path.resolve()),
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def _output_asset(self, url: str, step: Step, index: int, media_type: str | None) -> Asset:
        parsed, base = urlparse(url), urlparse(self._base_url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            # A public CDN link is kept as-is; the key is never sent to another host.
            validate_asset_url(url)
            return Asset(url=url, media_type=media_type or "application/octet-stream")
        headers = {"Authorization": self._headers()["Authorization"]}
        response = self._client.get(url, headers=headers, follow_redirects=False)
        if response.is_redirect:
            # Artifacts may redirect to signed storage; follow without the key.
            target = str(response.next_request.url) if response.next_request else ""
            validate_asset_url(target)
            response = self._client.get(target, follow_redirects=True)
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        return self._save(response.content, step, index, media_type or content_type)

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        try:
            image_data = self._image_results.pop(prediction_id, None)
            if image_data is not None:
                data = image_data
                items = data.get("data") or []
                for index, item in enumerate(items):
                    if item.get("b64_json"):
                        content = base64.b64decode(item["b64_json"])
                        step.assets.append(
                            self._save(content, step, index, _sniff_image_type(content))
                        )
                    elif item.get("url"):
                        step.assets.append(self._output_asset(item["url"], step, index, None))
                status = "succeeded"
            else:
                data = self._get_cached_poll_result(prediction_id) or self._get_job(prediction_id)
                status = str(data.get("status", "")).lower()
                if status in _FAILED_STATUSES:
                    error = data.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else error
                    raise ProviderError(
                        f"UberLLM video job {status}: {message or status}",
                        error_code=ProviderErrorCode.MODEL_ERROR,
                    )
                for index, result in enumerate(data.get("results") or []):
                    url = result.get("url")
                    if isinstance(url, str):
                        step.assets.append(
                            self._output_asset(url, step, index, result.get("content_type"))
                        )
            if not step.assets:
                raise ProviderError(
                    "UberLLM completed without outputs",
                    error_code=ProviderErrorCode.MODEL_ERROR,
                )
            usage = data.get("usage") or {}
            if isinstance(usage.get("cost_usd"), int | float):
                step.cost_usd = float(usage["cost_usd"])
            step.provider_payload = {
                "uberllm": {
                    "id": str(data.get("id") or prediction_id),
                    "model": data.get("model") or step.model,
                    "status": status,
                }
            }
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"UberLLM output fetch failed: {exc}",
                error_code=map_uberllm_error(exc),
            ) from exc
