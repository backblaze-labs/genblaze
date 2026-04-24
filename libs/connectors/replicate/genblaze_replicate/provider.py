"""ReplicateProvider — adapter for the Replicate API.

Models are free-form strings (any Replicate model slug). Default pricing is
compute-time-based (``predict_time`` × rate). Override per-model or wildcard::

    from genblaze_replicate import ReplicateProvider
    reg = ReplicateProvider.models_default().fork()
    reg.register_pricing("owner/my-model", per_response_metric(...))
    provider = ReplicateProvider(models=reg)
"""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    ModelRegistry,
    ModelSpec,
    PricingContext,
    ProviderCapabilities,
    per_response_metric,
    route_by_media_type,
    validate_asset_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_replicate_error

# Default per-second GPU-compute rate (Nvidia A40/T4 tier).
_COST_PER_SEC = 0.000225


def _compute_time_cost(ctx: PricingContext) -> float | None:
    payload = ctx.provider_payload.get("replicate") if ctx.provider_payload else None
    if not isinstance(payload, dict):
        return None
    predict_time = payload.get("predict_time")
    if predict_time is None:
        return None
    try:
        return float(predict_time) * _COST_PER_SEC
    except (TypeError, ValueError):
        return None


# Replicate has no enumerable model list; fallback spec applies to every model.
_FALLBACK_SPEC = ModelSpec(
    model_id="*",
    pricing=per_response_metric(_compute_time_cost),
    input_mapping=route_by_media_type({"image": "image", "video": "video", "audio": "audio"}),
)


class ReplicateProvider(BaseProvider):
    """Provider adapter for Replicate (replicate.com)."""

    name = "replicate"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        # No enumerable defaults — every model uses the fallback spec.
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
    ):
        super().__init__(models=models)
        self.poll_interval = poll_interval
        self._client: Any = None
        self._api_token = api_token
        self._http_timeout = http_timeout

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
