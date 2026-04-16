"""SoraProvider — adapter for the OpenAI Videos API (Sora).

Uses the asynchronous job-based Videos API:
  POST /v1/videos → poll GET /v1/videos/{id} → download content

Docs: https://platform.openai.com/docs/api-reference/videos
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from genblaze_openai._errors import map_openai_error

logger = logging.getLogger("genblaze.openai.sora")

# Valid Sora sizes (width x height)
_VALID_SIZES = {"720x1280", "1280x720", "1024x1792", "1792x1024"}
_VALID_SECONDS = {4, 8, 12}

# Per-video pricing by (model, resolution_bucket). Prices in USD.
_SORA_PRICING: dict[str, float] = {
    "sora-2": 0.10,
    "sora-2-pro": 0.40,
}


class SoraProvider(BaseProvider):
    """Provider adapter for OpenAI Sora video generation.

    Models: ``sora-2`` (fast, default) and ``sora-2-pro`` (high quality).

    Args:
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        poll_interval: Seconds between status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 60).
    """

    name = "openai-sora"

    # Map standard resolution + aspect_ratio to Sora's size format
    _RESOLUTION_TO_SIZE: dict[tuple[str, str], str] = {
        ("1080p", "16:9"): "1280x720",
        ("720p", "16:9"): "1280x720",
        ("1080p", "9:16"): "720x1280",
        ("720p", "9:16"): "720x1280",
    }

    def __init__(
        self,
        api_key: str | None = None,
        poll_interval: float = 5.0,
        http_timeout: float = 60.0,
        output_dir: str | Path | None = None,
    ):
        super().__init__()
        self.poll_interval = poll_interval
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def get_capabilities(self) -> ProviderCapabilities:
        """Sora: video generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=["sora-2", "sora-2-pro"],
            output_formats=["video/mp4"],
        )

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to Sora-native names."""
        p = dict(params)
        # duration → seconds
        if "duration" in p and "seconds" not in p:
            p["seconds"] = p.pop("duration")
        # resolution + aspect_ratio → size
        if "resolution" in p and "size" not in p:
            ar = p.get("aspect_ratio", "16:9")
            requested = p["resolution"]
            key = (requested, ar)
            if key in self._RESOLUTION_TO_SIZE:
                mapped = self._RESOLUTION_TO_SIZE[key]
                # Sora caps landscape at 720p — warn if user requested higher
                if requested == "1080p" and mapped == "1280x720":
                    logger.warning(
                        "Sora does not support 1080p for %s — downgrading to 720p (1280x720)",
                        ar,
                    )
                p["size"] = mapped
            p.pop("resolution", None)
            p.pop("aspect_ratio", None)
        return p

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError as exc:
                raise ProviderError(
                    "openai package not installed. Run: pip install openai"
                ) from exc
            kwargs: dict = {"timeout": self._http_timeout}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create a video generation job via POST /v1/videos."""
        client = self._get_client()
        try:
            params: dict = {"model": step.model, "prompt": step.prompt or ""}

            if "seconds" in step.params:
                seconds = int(step.params["seconds"])
                if seconds not in _VALID_SECONDS:
                    raise ProviderError(
                        f"Invalid seconds={seconds}. Must be one of {_VALID_SECONDS}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                params["seconds"] = seconds
            if "size" in step.params:
                size = step.params["size"]
                if size not in _VALID_SIZES:
                    raise ProviderError(
                        f"Invalid size={size}. Must be one of {_VALID_SIZES}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                params["size"] = size

            # Image-to-video: pass first input image URL if available
            if step.inputs:
                for inp in step.inputs:
                    validate_chain_input_url(inp.url)
                    if inp.media_type and inp.media_type.startswith("image/"):
                        params["image"] = inp.url
                        break

            response = client.videos.create(**params)
            return response.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Sora submit failed: {exc}",
                error_code=map_openai_error(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check video generation status via GET /v1/videos/{id}."""
        client = self._get_client()
        try:
            video = client.videos.retrieve(prediction_id)
            if video.status in ("completed", "failed"):
                self._cache_poll_result(prediction_id, video)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"Sora poll failed: {exc}",
                error_code=map_openai_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch completed video, download with auth, and save locally."""
        client = self._get_client()
        try:
            video = self._get_cached_poll_result(prediction_id)
            if video is None:
                video = client.videos.retrieve(prediction_id)

            step.provider_payload = {
                "openai": {
                    "video_id": video.id,
                    "model": video.model if hasattr(video, "model") else None,
                    "status": video.status,
                }
            }

            if video.status == "failed":
                error_msg = getattr(video, "error", None) or "Video generation failed"
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Content endpoint requires the API key in the Authorization header
            content = client.videos.content(prediction_id, variant="video")
            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}.mp4"
                content.write_to_file(str(out_path))
            else:
                fd, tmp = tempfile.mkstemp(suffix=".mp4")
                os.close(fd)
                out_path = Path(tmp)
                content.write_to_file(str(out_path))

            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type="video/mp4")
            asset.video = VideoMetadata(has_audio=False, codec="h264")
            step.assets.append(asset)

            price = _SORA_PRICING.get(step.model)
            if price is not None:
                step.cost_usd = price

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Sora fetch_output failed: {exc}",
                error_code=map_openai_error(exc),
            ) from exc
