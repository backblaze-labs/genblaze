"""DecartVideoProvider — adapter for the Decart Lucy video API.

Uses the decart Python SDK with async queue-based workflow:
  client.queue.submit() → poll status → download result

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Pricing is keyed off the ``resolution``
param (480p vs 720p).

Docs: https://docs.platform.decart.ai/
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core._utils import _run_async
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BoolSchema,
    EnumSchema,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    by_param,
)
from genblaze_core.providers.base import BaseProvider
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_decart_error

# Supported video models
_VIDEO_MODELS = (
    "lucy-pro-t2v",
    "lucy-pro-i2v",
    "lucy-pro-v2v",
    "lucy-2-v2v",
    "lucy-fast-v2v",
    "lucy-motion",
    "lucy-dev-i2v",
    "lucy-restyle-v2v",
)

_VALID_RESOLUTIONS = frozenset({"480p", "720p"})

# Per-generation pricing by resolution (USD). Default is the 480p tier —
# matches the historical "fall through to 480p when resolution is missing"
# behavior.
_VIDEO_PRICING: dict[str, float] = {
    "480p": 0.04,
    "720p": 0.08,
}


def _video_spec(model_id: str) -> ModelSpec:
    """Per-model spec — pricing keyed by the ``resolution`` param."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        pricing=by_param("resolution", _VIDEO_PRICING, default=_VIDEO_PRICING["480p"]),
        param_coercers={"enhance_prompt": bool},
        param_schemas={
            "resolution": EnumSchema(values=_VALID_RESOLUTIONS),
            "enhance_prompt": BoolSchema(),
        },
    )


class DecartVideoProvider(BaseProvider):
    """Provider adapter for Decart Lucy video generation.

    Models: ``lucy-pro-t2v``, ``lucy-pro-i2v``, ``lucy-dev-i2v``, etc.

    Auth: Set DECART_API_KEY env var or pass api_key.

    Args:
        api_key: Decart API key. Falls back to DECART_API_KEY env var.
        poll_interval: Seconds between job status polls (default 5).
        output_dir: Directory for output files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "decart"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(defaults={mid: _video_spec(mid) for mid in _VIDEO_MODELS})

    def get_capabilities(self) -> ProviderCapabilities:
        """Decart Lucy: video generation from text and image prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            resolutions=["480p", "720p"],
            output_formats=["video/mp4"],
            models=sorted(self._models.known()),
        )

    def __init__(
        self,
        api_key: str | None = None,
        poll_interval: float = 5.0,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
    ):
        super().__init__(models=models)
        self.poll_interval = poll_interval
        self._api_key = api_key
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                from decart import DecartClient
            except ImportError as exc:
                raise ProviderError(
                    "decart package not installed. Run: pip install decart"
                ) from exc
            kwargs: dict = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = DecartClient(**kwargs)
        return self._client

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit a video generation job to Decart."""
        client = self._get_client()
        try:
            from decart import models

            # Registry pipeline validates resolution enum and SSRF-checks inputs.
            payload = self.prepare_payload(step)

            params: dict = {
                "model": models.video(step.model),  # type: ignore[arg-type]
                "prompt": payload.get("prompt", step.prompt or ""),
            }

            if "resolution" in payload:
                params["resolution"] = payload["resolution"]
            if step.seed is not None:
                params["seed"] = step.seed
            if "enhance_prompt" in payload:
                params["enhance_prompt"] = payload["enhance_prompt"]

            # Image-to-video: Decart's `data` field expects the first input URL.
            # Chain-input SSRF validation already done by prepare_payload.
            if step.inputs and len(step.inputs) > 0:
                params["data"] = step.inputs[0].url

            job = _run_async(client.queue.submit(params))
            return job.job_id

        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Decart submit failed: {exc}",
                error_code=map_decart_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if a Decart video job is complete."""
        client = self._get_client()
        try:
            status = _run_async(client.queue.status(prediction_id))
            if status.status in ("completed", "failed"):
                self._cache_poll_result(prediction_id, status)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"Decart poll failed: {exc}",
                error_code=map_decart_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch the completed video output from Decart."""
        client = self._get_client()
        try:
            status = self._get_cached_poll_result(prediction_id)
            if status is None:
                status = _run_async(client.queue.status(prediction_id))

            step.provider_payload = {
                "decart": {
                    "job_id": prediction_id,
                    "status": status.status,
                }
            }

            if status.status == "failed":
                error_msg = getattr(status, "error", None) or "Generation failed"
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Download the video result
            result = _run_async(client.queue.result(prediction_id))

            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}.mp4"
            else:
                fd, tmp = tempfile.mkstemp(suffix=".mp4")
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(result.data)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type="video/mp4")
            # Populate video metadata with resolution if provided
            vm_kwargs: dict[str, Any] = {"has_audio": False}
            if "resolution" in step.params:
                vm_kwargs["resolution"] = step.params["resolution"]
            asset.video = VideoMetadata(**vm_kwargs)
            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Decart fetch_output failed: {exc}",
                error_code=map_decart_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
