"""GMICloudVideoProvider — video generation via the GMICloud request queue.

Auth: Set GMI_API_KEY env var or pass api_key= to the constructor.

Docs: https://docs.gmicloud.ai

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
built in ``genblaze_gmicloud.models.video``. Users can register new models or
override pricing via::

    provider = GMICloudVideoProvider(models=my_registry)
"""

from __future__ import annotations

from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, Track, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    ProviderCapabilities,
    validate_asset_url,
)
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.runnable.config import RunnableConfig

from ._base import GMICloudBase, extract_media_url
from ._errors import map_gmicloud_error
from .models.video import build_video_registry

# Canonical slugs for models that produce audio alongside video (multi-track).
# Legacy ids resolve through ``_resolve_model`` before this check.
_HAS_AUDIO_MODELS: frozenset[str] = frozenset({"veo3", "veo3-fast"})


class GMICloudVideoProvider(GMICloudBase):
    """Provider adapter for GMICloud video generation via the request queue.

    Models: Seedance, Kling, Veo, Sora, Wan, Minimax Hailuo, PixVerse,
    Luma Ray, Vidu, and any new model added to GMICloud's queue (unknown
    models pass through).

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "gmicloud"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return build_video_registry()

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        try:
            payload = self.prepare_payload(step)
            return self._submit_request(step.model, payload)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud submit failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        try:
            detail = self._fetch_detail(prediction_id)
            status = detail.get("status", "")
            outcome = detail.get("outcome") or {}

            step.provider_payload = {"gmicloud": {"request_id": prediction_id, "status": status}}

            if status in ("failed", "cancelled"):
                raise ProviderError(
                    str(detail.get("error") or f"Video generation {status}"),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            video_url = extract_media_url(outcome)
            if not video_url:
                raise ProviderError("GMICloud request completed but no video URL found")

            validate_asset_url(str(video_url))
            asset = Asset(url=str(video_url), media_type="video/mp4")

            has_audio = self._models.resolve_canonical(step.model) in _HAS_AUDIO_MODELS
            asset.video = VideoMetadata(has_audio=has_audio)
            if has_audio:
                asset.tracks = [
                    Track(kind="video", codec="h264"),
                    Track(kind="audio", codec="aac", label="generated-audio"),
                ]
                asset.audio = AudioMetadata(codec="aac")

            step.assets.append(asset)
            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud fetch_output failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc
