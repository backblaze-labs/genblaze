"""RunwayProvider — adapter for the Runway Gen video API.

Uses the runwayml Python SDK with async task-based workflow:
  client.image_to_video.create() → poll task → get output URL

Docs: https://docs.runwayml.com/
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

from ._errors import map_runway_error

_VALID_DURATIONS = {5, 10}
_VALID_RATIOS = {"16:9", "9:16"}

# Per-generation pricing by (model, duration) in USD
_PRICING: dict[tuple[str, int], float] = {
    ("gen4_turbo", 5): 0.50,
    ("gen4_turbo", 10): 1.00,
    ("gen3a_turbo", 5): 0.25,
    ("gen3a_turbo", 10): 0.50,
}


class RunwayProvider(BaseProvider):
    """Provider adapter for Runway video generation (Gen-3, Gen-4).

    Models: ``gen4_turbo``, ``gen3a_turbo``.

    Auth: Set RUNWAYML_API_SECRET env var or pass api_secret.

    Args:
        api_secret: Runway API secret. Falls back to RUNWAYML_API_SECRET env var.
        poll_interval: Seconds between task status polls (default 5).
    """

    name = "runway"

    def get_capabilities(self) -> ProviderCapabilities:
        """Runway: video generation from text and/or image inputs."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            max_duration=10.0,
            models=["gen4_turbo", "gen3a_turbo"],
            output_formats=["video/mp4"],
        )

    def __init__(self, api_secret: str | None = None, poll_interval: float = 5.0):
        super().__init__()
        self.poll_interval = poll_interval
        self._api_secret = api_secret
        self._client: Any = None

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to Runway-native names."""
        p = dict(params)
        # aspect_ratio → ratio (Runway's native key)
        if "aspect_ratio" in p and "ratio" not in p:
            p["ratio"] = p.pop("aspect_ratio")
        return p

    def _get_client(self):
        if self._client is None:
            try:
                from runwayml import RunwayML
            except ImportError as exc:
                raise ProviderError(
                    "runwayml package not installed. Run: pip install runwayml"
                ) from exc
            kwargs: dict = {}
            if self._api_secret:
                kwargs["api_key"] = self._api_secret
            self._client = RunwayML(**kwargs)
        return self._client

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create a video generation task."""
        client = self._get_client()
        try:
            params: dict = {
                "model": step.model,
                "prompt_text": step.prompt or "",
            }

            if "duration" in step.params:
                dur = int(step.params["duration"])
                if dur not in _VALID_DURATIONS:
                    raise ProviderError(
                        f"Invalid duration={dur}. Must be one of {_VALID_DURATIONS}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                params["duration"] = dur

            # normalize_params already mapped aspect_ratio → ratio
            ratio = step.params.get("ratio")
            if ratio:
                if ratio not in _VALID_RATIOS:
                    raise ProviderError(
                        f"Invalid ratio={ratio!r}. Must be one of {_VALID_RATIOS}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                params["ratio"] = ratio

            if step.seed is not None:
                params["seed"] = step.seed

            if "watermark" in step.params:
                params["watermark"] = bool(step.params["watermark"])

            # Image-to-video: pass image URL as prompt_image
            if step.inputs and len(step.inputs) > 0:
                validate_chain_input_url(step.inputs[0].url)
                params["prompt_image"] = step.inputs[0].url

            task = client.image_to_video.create(**params)
            return task.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Runway submit failed: {exc}",
                error_code=map_runway_error(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if the Runway task is complete."""
        client = self._get_client()
        try:
            task = client.tasks.retrieve(prediction_id)
            if task.status in ("SUCCEEDED", "FAILED"):
                self._cache_poll_result(prediction_id, task)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"Runway poll failed: {exc}",
                error_code=map_runway_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch the completed video URL."""
        client = self._get_client()
        try:
            task = self._get_cached_poll_result(prediction_id)
            if task is None:
                task = client.tasks.retrieve(prediction_id)

            step.provider_payload = {
                "runway": {
                    "task_id": task.id,
                    "status": task.status,
                }
            }

            if task.status == "FAILED":
                error_msg = getattr(task, "failure", None) or "Video generation failed"
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Task output contains the video URL
            output = getattr(task, "output", None)
            if output and isinstance(output, list) and len(output) > 0:
                url = str(output[0])
                validate_asset_url(url)
                step.assets.append(Asset(url=url, media_type="video/mp4"))
            elif output and isinstance(output, str):
                validate_asset_url(output)
                step.assets.append(Asset(url=output, media_type="video/mp4"))
            else:
                raise ProviderError("Runway task completed but no output URL found")

            duration = int(step.params.get("duration", 5))
            for a in step.assets:
                a.video = VideoMetadata(has_audio=False)
                a.duration = a.duration or float(duration)

            per_gen = _PRICING.get((step.model, duration))
            if per_gen is not None:
                step.cost_usd = per_gen * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Runway fetch_output failed: {exc}",
                error_code=map_runway_error(exc),
            ) from exc
