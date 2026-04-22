"""GMICloudVideoProvider — video generation via the GMICloud request queue.

Auth: Set GMI_API_KEY env var or pass api_key= to the constructor.

Docs: https://docs.gmicloud.ai
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
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from ._base import GMICloudBase
from ._errors import map_gmicloud_error

# Per-generation pricing by model (USD) — approximate, based on GMICloud tiers.
# Unknown models pass through to the API; cost_usd will be None.
_VIDEO_PRICING: dict[str, float] = {
    "Veo3": 0.40,
    "Veo3-Fast": 0.15,
    "Sora-2-Pro": 0.50,
    "Kling-Image2Video-V2.1-Master": 0.28,
    "Kling-Text2Video-V2.1-Master": 0.28,
    "Kling-Image2Video-V1.6-Pro": 0.098,
    "Kling-Text2Video-V1.6-Pro": 0.098,
    "Kling-Image2Video-V1.5-Pro": 0.098,
    "Kling-Text2Video-V1.5-Pro": 0.098,
    "Minimax-Hailuo-2.3-Fast": 0.032,
    "PixVerse-v5.6": 0.03,
    "Wan-2.6-T2V": 0.15,
    "Wan-2.6-I2V": 0.15,
    "Luma-Ray-2": 0.20,
    "Vidu-Q1": 0.10,
}

# Models that produce audio alongside video (multi-track output)
_HAS_AUDIO_MODELS: set[str] = {"Veo3", "Veo3-Fast"}


class GMICloudVideoProvider(GMICloudBase):
    """Provider adapter for GMICloud video generation via the request queue.

    Models: Kling, Veo, Sora, Wan, Minimax Hailuo, PixVerse, Luma Ray, Vidu,
    and any new model added to GMICloud's queue (unknown models pass through).

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
    """

    name = "gmicloud"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=sorted(_VIDEO_PRICING),
            output_formats=["video/mp4"],
        )

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        p = dict(params)
        if "duration" in p:
            p["duration"] = int(p["duration"])
        if "cfg_scale" not in p and "guidance_scale" in p:
            p["cfg_scale"] = p.pop("guidance_scale")
        return p

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        try:
            payload: dict = {}
            if step.prompt:
                payload["prompt"] = step.prompt

            for key in ("duration", "cfg_scale", "aspect_ratio"):
                if key in step.params:
                    payload[key] = step.params[key]

            # Pipeline hoists negative_prompt and seed out of params onto the
            # top-level Step fields, so read them there (params won't have them).
            if step.negative_prompt:
                payload["negative_prompt"] = step.negative_prompt
            if step.seed is not None:
                payload["seed"] = step.seed

            if step.inputs and len(step.inputs) > 0:
                validate_chain_input_url(step.inputs[0].url)
                payload["image"] = step.inputs[0].url

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

            video_url = outcome.get("video_url") or outcome.get("url")
            if not video_url:
                raise ProviderError("GMICloud request completed but no video URL found")

            validate_asset_url(str(video_url))
            asset = Asset(url=str(video_url), media_type="video/mp4")

            has_audio = step.model in _HAS_AUDIO_MODELS
            asset.video = VideoMetadata(has_audio=has_audio)
            if has_audio:
                asset.tracks = [
                    Track(kind="video", codec="h264"),
                    Track(kind="audio", codec="aac", label="generated-audio"),
                ]
                asset.audio = AudioMetadata(codec="aac")

            step.assets.append(asset)

            per_gen = _VIDEO_PRICING.get(step.model)
            if per_gen is not None:
                step.cost_usd = per_gen * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud fetch_output failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc
