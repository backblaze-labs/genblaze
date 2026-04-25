"""GMICloud video-model specs.

Slugs match the live request-queue API (case-sensitive lowercase). All models
share a POST envelope ``{"model": id, "payload": {...}}`` expressed via
``extras={"envelope_key": "payload"}``. Legacy PascalCase ids used before v0.3
are kept as ``deprecated_aliases``; drop in v0.4.

Per-model surfaces are composed via :class:`ParamSurface` so each model
declares the exact set of params it accepts. Pixverse (`pixverse-v5.6-*`)
gets ``quality`` (required by the upstream API but stripped by the previous
shared allowlist).

**Reconciliation (2026-04-24):** Several previously-registered defaults 404
against the live API and were removed pending upstream confirmation. Their
slugs remain resolvable via ``deprecated_aliases`` to surface a clear
``DeprecationWarning`` rather than a confusing 404. Restore them once the
upstream confirms availability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ParamSurface,
    PricingContext,
    PricingStrategy,
    per_unit,
    route_images,
)

# Default video surface — the universally meaningful video params + the GMI
# guidance_scale alias.
_VIDEO_BASE = (
    ParamSurface.for_modality(Modality.VIDEO)
    .with_aliases(guidance_scale="cfg_scale")
    .with_coercers(duration=int)
    .extend("cfg_scale")
)

# Pixverse models require ``quality`` per the upstream API. Without it in the
# allowlist the typed path drops it and the model is unusable.
_PIXVERSE = _VIDEO_BASE.extend("quality")

# Wan transition / r2v variants accept multiple keyframes via ``image_url``.
_WAN_REF = _VIDEO_BASE.extend("image_url", "tail_image_url")


@dataclass(frozen=True)
class _VideoModel:
    """Editorial row — one per model in the GMI video catalog."""

    slug: str
    pricing_factory: Callable[[], PricingStrategy]
    deprecated: frozenset[str] = field(default_factory=frozenset)
    suspected_dead: bool = False
    surface: ParamSurface = _VIDEO_BASE


def _per_duration_rate(rate: float) -> PricingStrategy:
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


def _flat(rate: float) -> Callable[[], PricingStrategy]:
    """Bind ``per_unit(rate)`` so the dataclass can store a thunk."""
    return lambda: per_unit(rate)


def _per_sec(rate: float) -> Callable[[], PricingStrategy]:
    return lambda: _per_duration_rate(rate)


# Each model carries a ``suspected_dead`` flag for slugs that the latest
# live-probe report flagged 404 — registry shape is preserved so user code
# pinned to those slugs keeps importing/typing, while the Phase 4 probe-CI
# will fail the release until upstream confirms or maintainers prune.
_VIDEO_MODELS: tuple[_VideoModel, ...] = (
    _VideoModel("seedance-1-0-pro-250528", _flat(0.30)),
    _VideoModel("seedance-1-0-pro-fast", _flat(0.022)),
    _VideoModel("seedance-2-0-260128", _per_sec(0.052)),
    _VideoModel("veo3", _flat(0.40), frozenset({"Veo3"})),
    _VideoModel("veo3-fast", _flat(0.15), frozenset({"Veo3-Fast"}), suspected_dead=True),
    _VideoModel("sora-2-pro", _flat(0.50), frozenset({"Sora-2-Pro"})),
    _VideoModel(
        "kling-image2video-v2.1-master",
        _flat(0.28),
        frozenset({"Kling-Image2Video-V2.1-Master"}),
    ),
    _VideoModel(
        "kling-text2video-v2.1-master",
        _flat(0.28),
        frozenset({"Kling-Text2Video-V2.1-Master"}),
        suspected_dead=True,
    ),
    _VideoModel(
        "kling-image2video-v1.6-pro", _flat(0.098), frozenset({"Kling-Image2Video-V1.6-Pro"})
    ),
    _VideoModel(
        "kling-text2video-v1.6-pro", _flat(0.098), frozenset({"Kling-Text2Video-V1.6-Pro"})
    ),
    _VideoModel(
        "kling-image2video-v1.5-pro", _flat(0.098), frozenset({"Kling-Image2Video-V1.5-Pro"})
    ),
    _VideoModel(
        "kling-text2video-v1.5-pro", _flat(0.098), frozenset({"Kling-Text2Video-V1.5-Pro"})
    ),
    _VideoModel(
        "minimax-hailuo-2.3-fast",
        _flat(0.032),
        frozenset({"Minimax-Hailuo-2.3-Fast"}),
        suspected_dead=True,
    ),
    _VideoModel("pixverse-v5.6-t2v", _flat(0.03), frozenset({"PixVerse-v5.6"}), surface=_PIXVERSE),
    _VideoModel("pixverse-v5.6-i2v", _flat(0.03), surface=_PIXVERSE),
    _VideoModel("pixverse-v5.6-transition", _flat(0.03), surface=_PIXVERSE),
    _VideoModel("wan2.6-t2v", _flat(0.15), frozenset({"Wan-2.6-T2V"})),
    _VideoModel("wan2.6-i2v", _flat(0.15), frozenset({"Wan-2.6-I2V"})),
    _VideoModel("wan2.6-r2v", _flat(0.15), surface=_WAN_REF),
    _VideoModel("wan2.7-t2v", _flat(0.15)),
    _VideoModel("wan2.7-i2v", _flat(0.15)),
    _VideoModel("luma-ray-2", _flat(0.20), frozenset({"Luma-Ray-2"})),
    _VideoModel("vidu-q1", _flat(0.10), frozenset({"Vidu-Q1"}), suspected_dead=True),
)

_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


def _video_spec(model: _VideoModel) -> ModelSpec:
    extras: dict[str, object] = dict(_ENVELOPE)
    if model.suspected_dead:
        extras["suspected_dead"] = True
    return ModelSpec(
        model_id=model.slug,
        modality=Modality.VIDEO,
        deprecated_aliases=model.deprecated,
        pricing=model.pricing_factory(),
        input_mapping=_COMMON_INPUT,
        extras=extras,
        **model.surface.build(),
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_aliases={"guidance_scale": "cfg_scale"},
    param_coercers={"duration": int},
    input_mapping=_COMMON_INPUT,
    extras=_ENVELOPE,
    # No pricing for unknown models — historical "pass through" behavior.
)


def build_video_registry() -> ModelRegistry:
    defaults = {m.slug: _video_spec(m) for m in _VIDEO_MODELS}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
