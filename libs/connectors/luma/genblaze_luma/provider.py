"""LumaProvider — adapter for the Luma Dream Machine video API.

Uses the lumaai Python SDK with async generation-based workflow:
  client.generations.create() → poll generation → get output URL

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships a
pattern-keyed ``luma-ray`` family (``^ray-``) instead of a hardcoded
slug list. New ``ray-N`` and ``ray-*-N`` slugs inherit the param
shape automatically.

**DiscoverySupport.NONE**: the lumaai SDK does not expose a
``GET /models`` endpoint or any way to probe a slug without
enqueuing a (billable) generation, so per-slug authoritative
liveness is not achievable. Mirrors the Runway / Decart precedent —
the small stable catalog plus submit-time errors are sufficient.

**Pricing**: Luma bills by duration; a (model, duration) formula
isn't shipped in the SDK. ``cost_usd`` stays ``None`` unless the
user registers a pricing strategy via
``provider.models.register_pricing(...)``. See
``docs/reference/pricing-recipes.md`` for the canonical Luma recipe.

Docs: https://docs.lumalabs.ai/
"""

from __future__ import annotations

import re
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    ModelFamily,
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


_LUMA_RAY_FAMILY = ModelFamily(
    name="luma-ray",
    pattern=re.compile(r"^ray-"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_coercers={"loop": _coerce_loop},
        param_constraints=(_check_aspect_ratio,),
        param_allowlist=_PARAM_ALLOWLIST,
        input_mapping=route_keyframes(frames=("frame0",)),
    ),
    description=(
        "Luma Ray family — Dream Machine text/keyframe-conditioned video "
        "generation. Covers ray-2, ray-flash-2, and future ray-N variants."
    ),
    example_slugs=("ray-2", "ray-flash-2"),
)


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_coercers={"loop": _coerce_loop},
    param_constraints=(_check_aspect_ratio,),
    param_allowlist=_PARAM_ALLOWLIST,
    input_mapping=route_keyframes(frames=("frame0",)),
)


class LumaProvider(BaseProvider):
    """Provider adapter for Luma Dream Machine video generation.

    Models match the ``luma-ray`` family (``^ray-``). Current
    examples: ``ray-2``, ``ray-flash-2``.

    Auth: Set LUMAAI_API_KEY env var or pass auth_token.

    Args:
        auth_token: Luma API key. Falls back to LUMAAI_API_KEY env var.
        poll_interval: Seconds between generation status polls (default 5).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL (no-op for NONE).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "luma"
    discovery_support = DiscoverySupport.NONE
    """The lumaai SDK exposes no per-slug liveness probe that doesn't
    enqueue a billable generation. Family-pattern resolution + a
    small stable catalog plus submit-time errors are sufficient —
    same call as Runway / Decart."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_LUMA_RAY_FAMILY,),
            fallback=_FALLBACK,
        )

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
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ):
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self._auth_token = auth_token
        self._client: Any = None
        # In-progress generations cached for poll_progress() so we don't
        # double the API call rate to surface preview frames.
        self._progress_cache: dict[str, Any] = {}

    def close(self) -> None:
        """Release the lazily built SDK client."""
        self._release_client()

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
            # Stash so poll_progress() can read intermediate preview frames
            # without a second API call.
            self._progress_cache[str(prediction_id)] = generation
            return False
        except Exception as exc:
            raise ProviderError(
                f"Luma poll failed: {exc}",
                error_code=map_luma_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def poll_progress(self, prediction_id: Any) -> dict[str, Any] | None:
        """Surface Luma intermediate preview frames if the generation has any.

        The Luma SDK exposes draft frames in ``generation.assets.image`` /
        ``generation.assets.preview`` on some Dream Machine models partway
        through generation. ``getattr`` is defensive against SDK versions
        that don't carry the field.
        """
        gen = self._progress_cache.get(str(prediction_id))
        if gen is None:
            return None
        signals: dict[str, Any] = {}
        assets = getattr(gen, "assets", None)
        if assets is not None:
            preview = (
                getattr(assets, "preview", None)
                or getattr(assets, "image", None)
                or getattr(assets, "thumbnail", None)
            )
            if preview:
                signals["preview_url"] = str(preview)
        # Luma exposes a status string but no numeric progress; surface the
        # state as a human-readable message so consumers can show it.
        state = getattr(gen, "state", None)
        if state:
            signals["message"] = str(state)
        return signals or None

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
