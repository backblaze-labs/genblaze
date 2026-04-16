"""ReplicateProvider — adapter for the Replicate API."""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_replicate_error

# Replicate per-second compute cost (USD) — varies by hardware but this
# is a reasonable default for GPU predictions (Nvidia A40/T4 tier).
_COST_PER_SEC = 0.000225


class ReplicateProvider(BaseProvider):
    """Provider adapter for Replicate (replicate.com)."""

    name = "replicate"

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
    ):
        super().__init__()
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
            input_params = {**step.params}
            if step.prompt:
                input_params["prompt"] = step.prompt
            if step.negative_prompt:
                input_params["negative_prompt"] = step.negative_prompt

            # Pass chain inputs as model-specific parameters by media type
            if step.inputs:
                for inp in step.inputs:
                    validate_chain_input_url(inp.url)
                    mt = inp.media_type or ""
                    if mt.startswith("image/") and "image" not in input_params:
                        input_params["image"] = inp.url
                    elif mt.startswith("video/") and "video" not in input_params:
                        input_params["video"] = inp.url
                    elif mt.startswith("audio/") and "audio" not in input_params:
                        input_params["audio"] = inp.url

            prediction = client.predictions.create(
                model=step.model,
                input=input_params,
            )
            return prediction.id
        except Exception as exc:
            raise ProviderError(
                f"Replicate submit failed: {exc}",
                error_code=map_replicate_error(exc),
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
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        client = self._get_client()
        try:
            prediction = self._get_cached_poll_result(prediction_id)
            if prediction is None:
                prediction = client.predictions.get(prediction_id)

            # Store raw provider response
            step.provider_payload = {
                "replicate": {
                    "prediction_id": prediction.id,
                    "model": prediction.model if hasattr(prediction, "model") else None,
                    "version": prediction.version if hasattr(prediction, "version") else None,
                    "status": prediction.status,
                    "created_at": str(prediction.created_at)
                    if hasattr(prediction, "created_at")
                    else None,
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

            output = prediction.output
            if isinstance(output, str):
                output = [output]
            elif output is None:
                output = []

            for url in output:
                url_str = str(url)
                validate_asset_url(url_str)
                # Infer MIME from URL extension; fall back to modality default
                path = urlparse(url_str).path
                mime, _ = mimetypes.guess_type(path)
                if mime is None:
                    mime = f"{step.modality.value}/octet-stream"
                step.assets.append(Asset(url=url_str, media_type=mime))

            # Track cost from prediction compute time
            metrics = getattr(prediction, "metrics", None)
            if metrics:
                predict_time = getattr(metrics, "predict_time", None)
                if predict_time is not None:
                    step.cost_usd = float(predict_time) * _COST_PER_SEC

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Replicate fetch_output failed: {exc}",
                error_code=map_replicate_error(exc),
            ) from exc
