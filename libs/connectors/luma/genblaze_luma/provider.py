"""LumaProvider — adapter for the Luma Dream Machine video API.

Uses the lumaai Python SDK with async generation-based workflow:
  client.generations.create() → poll generation → get output URL

Docs: https://docs.lumalabs.ai/
"""

from __future__ import annotations

from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_luma_error

_VALID_ASPECT_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9", "9:21"}

# Per-generation pricing by model tier (USD)
_PRICING: dict[str, float] = {
    "ray-2": 0.40,
    "ray-flash-2": 0.20,
}


class LumaProvider(BaseProvider):
    """Provider adapter for Luma Dream Machine video generation.

    Models: ``ray-2`` (latest), ``ray-flash-2`` (fast).

    Auth: Set LUMAAI_API_KEY env var or pass auth_token.

    Args:
        auth_token: Luma API key. Falls back to LUMAAI_API_KEY env var.
        poll_interval: Seconds between generation status polls (default 5).
    """

    name = "luma"

    def get_capabilities(self) -> ProviderCapabilities:
        """Luma: video generation from text or image prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=["ray-2", "ray-flash-2"],
            output_formats=["video/mp4"],
        )

    def __init__(self, auth_token: str | None = None, poll_interval: float = 5.0):
        super().__init__()
        self.poll_interval = poll_interval
        self._auth_token = auth_token
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                from lumaai import LumaAI
            except ImportError as exc:
                raise ProviderError(
                    "lumaai package not installed. Run: pip install lumaai"
                ) from exc
            kwargs: dict = {}
            if self._auth_token:
                kwargs["auth_token"] = self._auth_token
            self._client = LumaAI(**kwargs)
        return self._client

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create a video generation via Luma Dream Machine."""
        client = self._get_client()
        try:
            params: dict = {
                "model": step.model,
                "prompt": step.prompt or "",
            }

            if "aspect_ratio" in step.params:
                ar = step.params["aspect_ratio"]
                if ar not in _VALID_ASPECT_RATIOS:
                    raise ProviderError(
                        f"Invalid aspect_ratio={ar!r}. Must be one of {_VALID_ASPECT_RATIOS}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                params["aspect_ratio"] = ar

            if "loop" in step.params:
                params["loop"] = bool(step.params["loop"])

            if "resolution" in step.params:
                params["resolution"] = step.params["resolution"]

            if "duration" in step.params:
                params["duration"] = step.params["duration"]

            # Image-to-video: pass image URL as keyframe
            if step.inputs and len(step.inputs) > 0:
                validate_chain_input_url(step.inputs[0].url)
                params["keyframes"] = {
                    "frame0": {
                        "type": "image",
                        "url": step.inputs[0].url,
                    }
                }

            generation = client.generations.create(**params)
            return generation.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Luma submit failed: {exc}",
                error_code=map_luma_error(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if the Luma generation is complete."""
        client = self._get_client()
        try:
            generation = client.generations.get(prediction_id)
            if generation.state in ("completed", "failed"):
                self._cache_poll_result(prediction_id, generation)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"Luma poll failed: {exc}",
                error_code=map_luma_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch the completed video URL from Luma."""
        client = self._get_client()
        try:
            generation = self._get_cached_poll_result(prediction_id)
            if generation is None:
                generation = client.generations.get(prediction_id)

            step.provider_payload = {
                "luma": {
                    "generation_id": generation.id,
                    "state": generation.state,
                }
            }

            if generation.state == "failed":
                error_msg = (
                    getattr(generation, "failure_reason", None) or "Video generation failed"
                )
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            assets = getattr(generation, "assets", None)
            if assets:
                video_url = getattr(assets, "video", None)
                if video_url:
                    validate_asset_url(str(video_url))
                    asset = Asset(url=str(video_url), media_type="video/mp4")
                    asset.video = VideoMetadata(has_audio=False)
                    step.assets.append(asset)

                    per_gen = _PRICING.get(step.model)
                    if per_gen is not None:
                        step.cost_usd = per_gen * len(step.assets)

                    return step

            raise ProviderError("Luma generation completed but no video URL found")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Luma fetch_output failed: {exc}",
                error_code=map_luma_error(exc),
            ) from exc
