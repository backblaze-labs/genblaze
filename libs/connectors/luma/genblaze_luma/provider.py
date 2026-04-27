"""LumaProvider — adapter for the Luma Dream Machine video API.

Uses the lumaai Python SDK with async generation-based workflow:
  client.generations.create() → poll generation → get output URL

Models, parameter handling, and chain-input routing are driven by the
``ModelRegistry`` returned from ``create_registry()``. Pricing is
intentionally disabled — Luma bills by duration, and a per-(model, duration)
formula has not been implemented yet, so ``cost_usd`` stays ``None`` to avoid
misreporting. See ``test_cost_not_populated_until_formula_lands``.

Docs: https://docs.lumalabs.ai/
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
    RetryPolicy,
    route_keyframes,
    validate_asset_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_luma_error

_VALID_ASPECT_RATIOS = frozenset({"1:1", "3:4", "4:3", "9:16", "16:9", "21:9", "9:21"})

# Forwarded as-is to the Luma SDK; everything else is dropped.
_PARAM_ALLOWLIST = frozenset(
    {"prompt", "aspect_ratio", "loop", "resolution", "duration", "keyframes"}
)


def _check_aspect_ratio(params: dict[str, Any]) -> None:
    """Preserve the connector's bespoke 'Invalid aspect_ratio' error wording."""
    ar = params.get("aspect_ratio")
    if ar is not None and ar not in _VALID_ASPECT_RATIOS:
        raise ProviderError(
            f"Invalid aspect_ratio={ar!r}. Must be one of {set(_VALID_ASPECT_RATIOS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _coerce_loop(value: Any) -> bool:
    """Luma's ``loop`` is a strict bool; the connector historically coerced it."""
    return bool(value)


def _luma_spec(model_id: str) -> ModelSpec:
    """Build the per-model spec.

    Pricing is intentionally ``None`` — Luma bills by duration and the
    formula isn't implemented yet. Re-enable when a (model, duration) rate
    table lands; until then leaving this unset keeps ``cost_usd`` honest.
    """
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        pricing=None,
        param_coercers={"loop": _coerce_loop},
        param_constraints=(_check_aspect_ratio,),
        param_allowlist=_PARAM_ALLOWLIST,
        input_mapping=route_keyframes(frames=("frame0",)),
    )


class LumaProvider(BaseProvider):
    """Provider adapter for Luma Dream Machine video generation.

    Models: ``ray-2`` (latest), ``ray-flash-2`` (fast).

    Auth: Set LUMAAI_API_KEY env var or pass auth_token.

    Args:
        auth_token: Luma API key. Falls back to LUMAAI_API_KEY env var.
        poll_interval: Seconds between generation status polls (default 5).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "luma"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        defaults = {mid: _luma_spec(mid) for mid in ("ray-2", "ray-flash-2")}
        return ModelRegistry(defaults=defaults)

    def get_capabilities(self) -> ProviderCapabilities:
        """Luma: video generation from text or image prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        auth_token: str | None = None,
        poll_interval: float = 5.0,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(models=models, retry_policy=retry_policy)
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
            payload = self.prepare_payload(step)
            # The SDK insists on ``model``; the registry payload omits it.
            payload.setdefault("prompt", step.prompt or "")
            generation = client.generations.create(model=step.model, **payload)
            return generation.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Luma submit failed: {exc}",
                error_code=map_luma_error(exc),
                retry_after=retry_after_from_response(exc),
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
                retry_after=retry_after_from_response(exc),
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
                    self._apply_registry_pricing(step)
                    return step

            raise ProviderError("Luma generation completed but no video URL found")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Luma fetch_output failed: {exc}",
                error_code=map_luma_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
