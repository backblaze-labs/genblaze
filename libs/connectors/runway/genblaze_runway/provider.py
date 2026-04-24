"""RunwayProvider — adapter for the Runway Gen video API.

Uses the runwayml Python SDK with async task-based workflow:
  client.image_to_video.create() → poll task → get output URL

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Users can override pricing or register
new models via::

    provider = RunwayProvider(models=my_registry)

Docs: https://docs.runwayml.com/
"""

from __future__ import annotations

from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    by_model_and_param,
    route_images,
    validate_asset_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_runway_error

_VALID_DURATIONS = frozenset({5, 10})
_VALID_RATIOS = frozenset({"16:9", "9:16"})

# Per-generation pricing keyed by (model, duration) in USD.
_RUNWAY_PRICING: dict[tuple[str, Any], float] = {
    ("gen4_turbo", 5): 0.50,
    ("gen4_turbo", 10): 1.00,
    ("gen3a_turbo", 5): 0.25,
    ("gen3a_turbo", 10): 0.50,
}


def _check_ratio(params: dict[str, Any]) -> None:
    """Validate the (post-alias) Runway-native ``ratio`` value."""
    ratio = params.get("ratio")
    if ratio is not None and ratio not in _VALID_RATIOS:
        raise ProviderError(
            f"Invalid ratio={ratio!r}. Must be one of {set(_VALID_RATIOS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _check_duration(params: dict[str, Any]) -> None:
    """Validate ``duration`` with Runway-specific error wording."""
    if "duration" not in params:
        return
    try:
        dur = int(params["duration"])
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            f"Invalid duration={params['duration']!r}. Must be one of {set(_VALID_DURATIONS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        ) from exc
    if dur not in _VALID_DURATIONS:
        raise ProviderError(
            f"Invalid duration={dur}. Must be one of {set(_VALID_DURATIONS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    params["duration"] = dur


def _runway_spec(model_id: str) -> ModelSpec:
    """Build the per-model spec.

    All Runway models share the same shape: ``aspect_ratio`` aliases to
    ``ratio``, ``duration`` must be 5 or 10, the first chained image becomes
    ``prompt_image``, and pricing is keyed on ``(model, duration)``.
    """
    # Validation lives in ``param_constraints`` rather than ``param_schemas``
    # so the connector can keep its bespoke "Invalid duration=…" wording the
    # public tests assert on.
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        pricing=by_model_and_param("duration", _RUNWAY_PRICING),
        param_aliases={"aspect_ratio": "ratio"},
        param_constraints=(_check_duration, _check_ratio),
        input_mapping=route_images(slots=("prompt_image",)),
    )


class RunwayProvider(BaseProvider):
    """Provider adapter for Runway video generation (Gen-3, Gen-4).

    Models: ``gen4_turbo``, ``gen3a_turbo``.

    Auth: Set RUNWAYML_API_SECRET env var or pass api_secret.

    Args:
        api_secret: Runway API secret. Falls back to RUNWAYML_API_SECRET env var.
        poll_interval: Seconds between task status polls (default 5).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "runway"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        defaults = {mid: _runway_spec(mid) for mid in ("gen4_turbo", "gen3a_turbo")}
        return ModelRegistry(defaults=defaults)

    def get_capabilities(self) -> ProviderCapabilities:
        """Runway: video generation from text and/or image inputs."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            max_duration=10.0,
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        api_secret: str | None = None,
        poll_interval: float = 5.0,
        *,
        models: ModelRegistry | None = None,
    ):
        super().__init__(models=models)
        self.poll_interval = poll_interval
        self._api_secret = api_secret
        self._client: Any = None

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to Runway-native names.

        Kept for backward compatibility with callers that invoke it directly;
        ``prepare_payload`` also performs the alias via the model spec.
        """
        p = dict(params)
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
            payload = self.prepare_payload(step)

            # Translate canonical 'prompt' to Runway's 'prompt_text'; only the
            # SDK-recognized keys are forwarded to image_to_video.create.
            request: dict = {
                "model": step.model,
                "prompt_text": payload.get("prompt", step.prompt or ""),
            }
            for key in ("duration", "ratio", "seed", "watermark", "prompt_image"):
                if key in payload:
                    request[key] = payload[key]
            if "watermark" in request:
                request["watermark"] = bool(request["watermark"])

            task = client.image_to_video.create(**request)
            return task.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Runway submit failed: {exc}",
                error_code=map_runway_error(exc),
                retry_after=retry_after_from_response(exc),
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
                retry_after=retry_after_from_response(exc),
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

            # Pricing is keyed on (model, duration); default duration is 5s
            # when the user didn't specify one. Mutate step.params so the
            # registry strategy sees the effective value, matching the legacy
            # behavior where 5s was assumed for cost.
            duration = int(step.params.get("duration", 5))
            step.params.setdefault("duration", duration)
            for a in step.assets:
                a.video = VideoMetadata(has_audio=False)
                a.duration = a.duration or float(duration)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Runway fetch_output failed: {exc}",
                error_code=map_runway_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
