"""GMICloudVideoProvider — adapter for the GMICloud video request queue API.

Uses the gmicloud Python SDK with async queue-based workflow:
  video_manager.create_request() → poll status → get output

Docs: https://github.com/GMISWE/python-sdk
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

from ._errors import map_gmicloud_error

# Known video models on GMICloud's request queue
_VIDEO_MODELS = {
    "Kling-Image2Video-V1.6-Pro",
    "Kling-Text2Video-V1.6-Pro",
    "Kling-Image2Video-V1.5-Pro",
    "Kling-Text2Video-V1.5-Pro",
}

# Per-generation pricing by model (USD) — approximate, based on GMICloud tiers
_PRICING: dict[str, float] = {
    "Kling-Image2Video-V1.6-Pro": 0.30,
    "Kling-Text2Video-V1.6-Pro": 0.30,
    "Kling-Image2Video-V1.5-Pro": 0.20,
    "Kling-Text2Video-V1.5-Pro": 0.20,
}


class GMICloudVideoProvider(BaseProvider):
    """Provider adapter for GMICloud video generation via the request queue.

    Models: Kling video models (image-to-video, text-to-video).

    Auth: Set GMI_CLOUD_EMAIL and GMI_CLOUD_PASSWORD env vars, or pass
    them directly. The gmicloud SDK handles JWT session management.

    Args:
        email: GMICloud account email. Falls back to GMI_CLOUD_EMAIL env var.
        password: GMICloud account password. Falls back to GMI_CLOUD_PASSWORD env var.
        poll_interval: Seconds between request status polls (default 5).
    """

    name = "gmicloud"

    def get_capabilities(self) -> ProviderCapabilities:
        """GMICloud: video generation from text and image prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=sorted(_VIDEO_MODELS),
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        poll_interval: float = 5.0,
    ):
        super().__init__()
        self.poll_interval = poll_interval
        self._email = email
        self._password = password
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                from gmicloud import Client
            except ImportError as exc:
                raise ProviderError(
                    "gmicloud package not installed. Run: pip install gmicloud"
                ) from exc
            kwargs: dict = {}
            if self._email:
                kwargs["email"] = self._email
            if self._password:
                kwargs["password"] = self._password
            self._client = Client(**kwargs)
        return self._client

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to GMICloud payload keys."""
        p = dict(params)
        # duration → duration (same name, but ensure int)
        if "duration" in p:
            p["duration"] = int(p["duration"])
        # aspect_ratio → aspect_ratio (passthrough)
        if "cfg_scale" not in p and "guidance_scale" in p:
            p["cfg_scale"] = p.pop("guidance_scale")
        return p

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit a video generation request to GMICloud."""
        client = self._get_client()
        try:
            video_mgr = client.video_manager

            payload: dict = {}
            if step.prompt:
                payload["prompt"] = step.prompt

            # Forward supported params into the payload
            for key in ("duration", "cfg_scale", "aspect_ratio", "negative_prompt"):
                if key in step.params:
                    payload[key] = step.params[key]

            if step.seed is not None:
                payload["seed"] = step.seed

            # Image-to-video: pass image URL in payload
            if step.inputs and len(step.inputs) > 0:
                validate_chain_input_url(step.inputs[0].url)
                payload["image"] = step.inputs[0].url

            request = video_mgr.create_request(
                model=step.model,
                payload=payload,
            )
            return request.request_id

        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud submit failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if the GMICloud video request is complete."""
        client = self._get_client()
        try:
            detail = client.video_manager.get_request_detail(prediction_id)
            if detail.status in ("success", "failed", "cancelled"):
                self._cache_poll_result(prediction_id, detail)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"GMICloud poll failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch the completed video output from GMICloud."""
        client = self._get_client()
        try:
            detail = self._get_cached_poll_result(prediction_id)
            if detail is None:
                detail = client.video_manager.get_request_detail(prediction_id)

            step.provider_payload = {
                "gmicloud": {
                    "request_id": prediction_id,
                    "status": detail.status,
                }
            }

            if detail.status in ("failed", "cancelled"):
                error_msg = getattr(detail, "error", None) or f"Video generation {detail.status}"
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Extract output URL from outcome dict
            outcome = getattr(detail, "outcome", None) or {}
            video_url = None
            if isinstance(outcome, dict):
                video_url = outcome.get("video_url") or outcome.get("url")
            elif hasattr(outcome, "video_url"):
                video_url = outcome.video_url

            if not video_url:
                raise ProviderError("GMICloud request completed but no video URL found")

            validate_asset_url(str(video_url))
            asset = Asset(url=str(video_url), media_type="video/mp4")
            asset.video = VideoMetadata(has_audio=False)
            step.assets.append(asset)

            # Track cost by model
            per_gen = _PRICING.get(step.model)
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
