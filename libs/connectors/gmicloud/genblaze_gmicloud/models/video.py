"""GMICloud video-model specs.

Slugs match the live request-queue API (case-sensitive lowercase). All models
share a POST envelope ``{"model": id, "payload": {...}}`` expressed via
``extras={"envelope_key": "payload"}``. Legacy PascalCase ids used before v0.3
are kept as ``deprecated_aliases``; drop in v0.4.
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

# (canonical_slug, flat_usd_per_asset, {deprecated_legacy_ids})
_VIDEO_FLAT: tuple[tuple[str, float, frozenset[str]], ...] = (
    ("seedance-1-0-pro-250528", 0.30, frozenset()),
    ("seedance-1-0-pro-fast", 0.022, frozenset()),
    ("veo3", 0.40, frozenset({"Veo3"})),
    ("veo3-fast", 0.15, frozenset({"Veo3-Fast"})),
    ("sora-2-pro", 0.50, frozenset({"Sora-2-Pro"})),
    ("kling-image2video-v2.1-master", 0.28, frozenset({"Kling-Image2Video-V2.1-Master"})),
    ("kling-text2video-v2.1-master", 0.28, frozenset({"Kling-Text2Video-V2.1-Master"})),
    ("kling-image2video-v1.6-pro", 0.098, frozenset({"Kling-Image2Video-V1.6-Pro"})),
    ("kling-text2video-v1.6-pro", 0.098, frozenset({"Kling-Text2Video-V1.6-Pro"})),
    ("kling-image2video-v1.5-pro", 0.098, frozenset({"Kling-Image2Video-V1.5-Pro"})),
    ("kling-text2video-v1.5-pro", 0.098, frozenset({"Kling-Text2Video-V1.5-Pro"})),
    ("minimax-hailuo-2.3-fast", 0.032, frozenset({"Minimax-Hailuo-2.3-Fast"})),
    ("pixverse-v5.6-t2v", 0.03, frozenset({"PixVerse-v5.6"})),
    ("pixverse-v5.6-i2v", 0.03, frozenset()),
    ("pixverse-v5.6-transition", 0.03, frozenset()),
    ("wan2.6-t2v", 0.15, frozenset({"Wan-2.6-T2V"})),
    ("wan2.6-i2v", 0.15, frozenset({"Wan-2.6-I2V"})),
    ("wan2.6-r2v", 0.15, frozenset()),
    ("wan2.7-t2v", 0.15, frozenset()),
    ("wan2.7-i2v", 0.15, frozenset()),
    ("luma-ray-2", 0.20, frozenset({"Luma-Ray-2"})),
    ("vidu-q1", 0.10, frozenset({"Vidu-Q1"})),
)

# Per-second pricing (USD/sec of requested duration, × output count).
_VIDEO_PER_SECOND: tuple[tuple[str, float, frozenset[str]], ...] = (
    ("seedance-2-0-260128", 0.052, frozenset()),
)


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


def _video_spec(model_id: str, flat_rate: float, deprecated: frozenset[str]) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        deprecated_aliases=deprecated,
        pricing=per_unit(flat_rate),
        param_aliases=_COMMON_ALIASES,
        param_coercers=_COMMON_COERCERS,
        param_allowlist=_COMMON_ALLOWLIST,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
    )


def _video_spec_per_second(model_id: str, rate: float, deprecated: frozenset[str]) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        deprecated_aliases=deprecated,
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
        **{mid: _video_spec(mid, rate, dep) for mid, rate, dep in _VIDEO_FLAT},
        **{mid: _video_spec_per_second(mid, rate, dep) for mid, rate, dep in _VIDEO_PER_SECOND},
    }
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
