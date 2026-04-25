"""GMICloud image-model specs.

Slugs match the live request-queue API (case-sensitive lowercase). Legacy
PascalCase ids used before v0.3 are kept as ``deprecated_aliases`` — they
resolve to the canonical slug and emit a ``DeprecationWarning``. Drop in v0.4.

Per-model param surfaces are composed via :class:`ParamSurface` so each model
declares the exact set of params its upstream accepts. Bria inpaint models
get ``mask`` / ``mask_url``; Reve edit/remix variants get ``image_url`` /
``strength``; the rest fall back to the modality defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ParamSurface,
    per_unit,
    route_images,
)

# Default image surface — the universally meaningful image params plus the GMI
# request-queue extras (`number_of_images`, `image`).
_IMAGE_BASE = ParamSurface.for_modality(Modality.IMAGE).extend("number_of_images")

# Bria inpaint models (`bria-genfill`, `bria-eraser`) need a mask. Without
# these in the allowlist the typed path strips them and the model is unusable.
_BRIA_INPAINT = _IMAGE_BASE.extend("mask", "mask_url", "denoise", "strength")

# Reve edit / remix variants accept a strength + edit-specific reference image.
_REVE_EDIT = _IMAGE_BASE.extend("image_url", "strength")


@dataclass(frozen=True)
class _ImageModel:
    """Editorial row — one per model in the GMI image catalog."""

    slug: str
    flat_rate: float
    deprecated: frozenset[str] = field(default_factory=frozenset)
    surface: ParamSurface = _IMAGE_BASE


_IMAGE_MODELS: tuple[_ImageModel, ...] = (
    _ImageModel("seedream-5.0-lite", 0.035, frozenset({"Seedream-5.0-Lite"})),
    _ImageModel("gemini-2.5-flash-image", 0.039, frozenset({"Gemini-2.5-Flash-Image"})),
    _ImageModel("flux-kontext-pro", 0.05, frozenset({"FLUX-Kontext-Pro"})),
    _ImageModel("seededit-3-0-i2i-250628", 0.03, frozenset({"Seededit"}), surface=_REVE_EDIT),
    # Reve family — fast/normal, create/edit/remix variants.
    _ImageModel("reve-create-20250915", 0.007),
    _ImageModel("reve-edit-20250915", 0.007, surface=_REVE_EDIT),
    _ImageModel(
        "reve-edit-fast-20251030", 0.007, frozenset({"Reve-Edit-Fast"}), surface=_REVE_EDIT
    ),
    _ImageModel("reve-remix-20250915", 0.007, surface=_REVE_EDIT),
    _ImageModel("reve-remix-fast-20251030", 0.007, surface=_REVE_EDIT),
    # Bria fibo family — Blending/Relighting/Restoration are the legacy ids of
    # three specific operations; other fibo variants have no prior name.
    _ImageModel("bria-fibo-image-blend", 0.02, frozenset({"Bria-Blending"})),
    _ImageModel("bria-fibo-relight", 0.02, frozenset({"Bria-Relighting"})),
    _ImageModel("bria-fibo-restore", 0.02, frozenset({"Bria-Restoration"})),
    _ImageModel("bria-genfill", 0.02, surface=_BRIA_INPAINT),
    _ImageModel("bria-eraser", 0.02, surface=_BRIA_INPAINT),
)

_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


def _image_spec(model: _ImageModel) -> ModelSpec:
    return ModelSpec(
        model_id=model.slug,
        modality=Modality.IMAGE,
        deprecated_aliases=model.deprecated,
        pricing=per_unit(model.flat_rate),
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
        **model.surface.build(),
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.IMAGE,
    input_mapping=_COMMON_INPUT,
    extras=_ENVELOPE,
)


def build_image_registry() -> ModelRegistry:
    defaults = {m.slug: _image_spec(m) for m in _IMAGE_MODELS}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
