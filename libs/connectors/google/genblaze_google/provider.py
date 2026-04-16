"""VeoProvider — adapter for Google Veo video generation.

Uses the google-genai SDK with the async operation-based workflow:
  client.models.generate_videos() → poll operation → download video

Docs: https://ai.google.dev/gemini-api/docs/video
"""

from __future__ import annotations

from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, Track, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities, validate_asset_url
from genblaze_core.runnable.config import RunnableConfig

from genblaze_google._errors import map_google_error

# Valid Veo parameter values
_VALID_ASPECT_RATIOS = {"16:9", "9:16"}
_VALID_RESOLUTIONS = {"720p", "1080p", "4k"}
_VALID_DURATIONS = {"4", "6", "8"}

# Per-second pricing by model (USD)
_VEO_PRICING: dict[str, float] = {
    "veo-2.0-generate-001": 0.35,
    "veo-3.0-generate-001": 0.50,
    "veo-3.0-fast-generate-001": 0.25,
}


class VeoProvider(BaseProvider):
    """Provider adapter for Google Veo video generation.

    Models: ``veo-2.0-generate-001`` (stable), ``veo-3.0-generate-001`` (GA, audio),
    ``veo-3.0-fast-generate-001`` (GA, fast).

    Supports both Gemini API (api_key) and Vertex AI (project/location) auth.

    Args:
        api_key: Gemini API key. Falls back to GEMINI_API_KEY env var.
        project: GCP project ID for Vertex AI auth (mutually exclusive with api_key).
        location: GCP region for Vertex AI (default "us-central1").
        poll_interval: Seconds between operation polls (default 10).
    """

    name = "google-veo"

    def get_capabilities(self) -> ProviderCapabilities:
        """Veo: video generation from text prompts with configurable resolution and duration."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text"],
            max_duration=8.0,
            resolutions=["720p", "1080p", "4k"],
            models=[
                "veo-2.0-generate-001",
                "veo-3.0-generate-001",
                "veo-3.0-fast-generate-001",
            ],
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project: str | None = None,
        location: str = "us-central1",
        poll_interval: float = 10.0,
    ):
        super().__init__()
        self.poll_interval = poll_interval
        self._api_key = api_key
        self._project = project
        self._location = location
        self._client: Any = None

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to Veo-native names."""
        p = dict(params)
        # duration → duration_seconds (Veo's native key)
        if "duration" in p and "duration_seconds" not in p:
            p["duration_seconds"] = p.pop("duration")
        return p

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ProviderError(
                    "google-genai package not installed. Run: pip install google-genai"
                ) from exc

            if self._project:
                # Vertex AI auth
                self._client = genai.Client(
                    vertexai=True,
                    project=self._project,
                    location=self._location,
                )
            else:
                # Gemini API key auth
                kwargs: dict = {}
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                self._client = genai.Client(**kwargs)
        return self._client

    def _build_config(self, step: Step) -> Any:
        """Build a GenerateVideosConfig from step.params."""
        from google.genai import types

        config_kwargs: dict = {}

        if "aspect_ratio" in step.params:
            ar = step.params["aspect_ratio"]
            if ar not in _VALID_ASPECT_RATIOS:
                raise ProviderError(
                    f"Invalid aspect_ratio={ar!r}. Must be one of {_VALID_ASPECT_RATIOS}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            config_kwargs["aspect_ratio"] = ar

        if "resolution" in step.params:
            res = step.params["resolution"]
            if res not in _VALID_RESOLUTIONS:
                raise ProviderError(
                    f"Invalid resolution={res!r}. Must be one of {_VALID_RESOLUTIONS}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            config_kwargs["resolution"] = res

        # Duration (normalize_params already mapped duration → duration_seconds)
        raw_dur = step.params.get("duration_seconds")
        if raw_dur is not None:
            dur = str(raw_dur)
            if dur not in _VALID_DURATIONS:
                raise ProviderError(
                    f"Invalid duration_seconds={dur!r}. Must be one of {_VALID_DURATIONS}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            config_kwargs["duration_seconds"] = dur

        if "person_generation" in step.params:
            config_kwargs["person_generation"] = step.params["person_generation"]

        if "number_of_videos" in step.params:
            config_kwargs["number_of_videos"] = int(step.params["number_of_videos"])

        if "enhance_prompt" in step.params:
            config_kwargs["enhance_prompt"] = bool(step.params["enhance_prompt"])

        if step.seed is not None:
            config_kwargs["seed"] = step.seed

        return types.GenerateVideosConfig(**config_kwargs) if config_kwargs else None

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Start a video generation operation."""
        client = self._get_client()
        try:
            gen_config = self._build_config(step)
            kwargs: dict = {
                "model": step.model,
                "prompt": step.prompt or "",
            }
            if gen_config is not None:
                kwargs["config"] = gen_config

            operation = client.models.generate_videos(**kwargs)
            # Return the provider-native operation name for resume() compatibility
            return operation.name
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Veo submit failed: {exc}",
                error_code=map_google_error(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if the video generation operation is done."""
        client = self._get_client()
        try:
            operation = client.operations.get(prediction_id)
            if operation.done:
                self._cache_poll_result(prediction_id, operation)
                return True
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Veo poll failed: {exc}",
                error_code=map_google_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Download generated video(s) and attach asset URLs."""
        client = self._get_client()
        try:
            # Use cached poll result if available, otherwise fetch fresh
            operation = self._get_cached_poll_result(prediction_id)
            if operation is None:
                operation = client.operations.get(prediction_id)

            # Store provider metadata
            step.provider_payload = {
                "google": {
                    "operation_name": getattr(operation, "name", None),
                    "model": step.model,
                }
            }

            # Check for errors in the operation result
            if hasattr(operation, "error") and operation.error:
                raise ProviderError(
                    str(operation.error),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            response = operation.response
            if response is None or not hasattr(response, "generated_videos"):
                raise ProviderError("No video generated in response")

            # Veo 3.0 models generate audio alongside video
            is_veo3 = step.model.startswith("veo-3")

            for gv in response.generated_videos:
                video = gv.video
                # Download to get the file URI
                client.files.download(file=video)
                # Use the video's URI as the asset URL
                video_uri = getattr(video, "uri", None)
                if video_uri:
                    validate_asset_url(video_uri)
                    vm_kwargs: dict[str, Any] = {"has_audio": is_veo3}
                    if "resolution" in step.params:
                        vm_kwargs["resolution"] = step.params["resolution"]
                    asset = Asset(url=video_uri, media_type="video/mp4")
                    asset.video = VideoMetadata(**vm_kwargs)
                    # Multi-track metadata for Veo 3 (video + generated audio)
                    if is_veo3:
                        asset.tracks = [
                            Track(kind="video", codec="h264"),
                            Track(kind="audio", codec="aac", label="generated-audio"),
                        ]
                        asset.audio = AudioMetadata(codec="aac")
                    step.assets.append(asset)
                else:
                    # Fallback: save locally and use file path
                    raise ProviderError(
                        "Veo response missing video URI — "
                        "use client.files.download() to save locally"
                    )

            # Track cost — accept both native and standard duration key
            price_per_sec = _VEO_PRICING.get(step.model)
            if price_per_sec is not None:
                raw_dur = step.params.get("duration_seconds") or step.params.get("duration")
                duration = int(raw_dur) if raw_dur is not None else 4
                step.cost_usd = price_per_sec * duration * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Veo fetch_output failed: {exc}",
                error_code=map_google_error(exc),
            ) from exc
