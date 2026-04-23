"""GMICloud video-model specs.

All models share a POST envelope ``{"model": id, "payload": {...}}`` — expressed
via ``extras={"envelope_key": "payload"}`` at the registry level (connector
reads this before HTTP submit).
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    PricingContext,
    per_unit,
    route_images,
)

# Flat per-generation pricing (USD per asset).
_VIDEO_FLAT: dict[str, float] = {
    "seedance-1-0-pro-250528": 0.30,
    "seedance-1-0-pro-fast": 0.022,
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

# Per-second pricing (USD/sec of requested duration, × output count).
_VIDEO_PER_SECOND: dict[str, float] = {
    "seedance-2-0-260128": 0.052,
}


def _per_duration_rate(rate: float):
    """Pricing strategy: ``rate × duration × output_count`` from ``step.params["duration"]``."""

    def _s(ctx: PricingContext) -> float | None:
        dur = ctx.step.params.get("duration")
        if dur is None:
            return None
        try:
            dur_f = float(dur)
        except (TypeError, ValueError):
            return None
        n = ctx.output_count or 1
        return rate * dur_f * n

    return _s


# Shared param shape for most GMI video models.
_COMMON_ALLOWLIST = frozenset(
    {
        "prompt",
        "negative_prompt",
        "seed",
        "duration",
        "cfg_scale",
        "aspect_ratio",
        "image",
    }
)

_COMMON_ALIASES = {"guidance_scale": "cfg_scale"}
_COMMON_COERCERS = {"duration": int}
_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


def _video_spec(model_id: str, flat_rate: float) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        pricing=per_unit(flat_rate),
        param_aliases=_COMMON_ALIASES,
        param_coercers=_COMMON_COERCERS,
        param_allowlist=_COMMON_ALLOWLIST,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
    )


def _video_spec_per_second(model_id: str, rate: float) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        pricing=_per_duration_rate(rate),
        param_aliases=_COMMON_ALIASES,
        param_coercers=_COMMON_COERCERS,
        param_allowlist=_COMMON_ALLOWLIST,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_aliases=_COMMON_ALIASES,
    param_coercers=_COMMON_COERCERS,
    input_mapping=_COMMON_INPUT,
    extras=_ENVELOPE,
    # No pricing for unknown models — historical "pass through" behavior.
)


def build_video_registry() -> ModelRegistry:
    defaults = {
        **{mid: _video_spec(mid, rate) for mid, rate in _VIDEO_FLAT.items()},
        **{mid: _video_spec_per_second(mid, rate) for mid, rate in _VIDEO_PER_SECOND.items()},
    }
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
