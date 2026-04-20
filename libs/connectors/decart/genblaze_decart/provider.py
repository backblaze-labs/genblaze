"""DecartVideoProvider — adapter for the Decart Lucy video API.

Uses the decart Python SDK with async queue-based workflow:
  client.queue.submit() → poll status → download result

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
from genblaze_core.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_decart_error

# Supported video models
_VIDEO_MODELS = {
    "lucy-pro-t2v",
    "lucy-pro-i2v",
    "lucy-pro-v2v",
    "lucy-2-v2v",
    "lucy-fast-v2v",
    "lucy-motion",
    "lucy-dev-i2v",
    "lucy-restyle-v2v",
}

_VALID_RESOLUTIONS = {"480p", "720p"}

# Per-generation pricing by resolution (USD)
_VIDEO_PRICING: dict[str, float] = {
    "480p": 0.04,
    "720p": 0.08,
}


class DecartVideoProvider(BaseProvider):
    """Provider adapter for Decart Lucy video generation.

    Models: ``lucy-pro-t2v``, ``lucy-pro-i2v``, ``lucy-dev-i2v``.

    Auth: Set DECART_API_KEY env var or pass api_key.

    Args:
        api_key: Decart API key. Falls back to DECART_API_KEY env var.
        poll_interval: Seconds between job status polls (default 5).
        output_dir: Directory for output files (default system temp).
    """

    name = "decart"

    def get_capabilities(self) -> ProviderCapabilities:
        """Decart Lucy: video generation from text and image prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            resolutions=["480p", "720p"],
            output_formats=["video/mp4"],
            models=sorted(_VIDEO_MODELS),
        )

    def __init__(
        self,
        api_key: str | None = None,
        poll_interval: float = 5.0,
        output_dir: str | Path | None = None,
    ):
        super().__init__()
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

            params: dict = {
                "model": models.video(step.model),  # type: ignore[arg-type]
                "prompt": step.prompt or "",
            }

            if "resolution" in step.params:
                params["resolution"] = step.params["resolution"]
            if step.seed is not None:
                params["seed"] = step.seed
            if "enhance_prompt" in step.params:
                params["enhance_prompt"] = bool(step.params["enhance_prompt"])

            # Image-to-video: pass input image data
            if step.inputs and len(step.inputs) > 0:
                validate_chain_input_url(step.inputs[0].url)
                params["data"] = step.inputs[0].url

            job = _run_async(client.queue.submit(params))
            return job.job_id

        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Decart submit failed: {exc}",
                error_code=map_decart_error(exc),
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

            # Track cost by resolution
            resolution = step.params.get("resolution", "480p")
            per_gen = _VIDEO_PRICING.get(resolution)
            if per_gen is not None:
                step.cost_usd = per_gen * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Decart fetch_output failed: {exc}",
                error_code=map_decart_error(exc),
            ) from exc
