"""GMICloud image-model specs.

Slugs match the live request-queue API (case-sensitive lowercase). Legacy
PascalCase ids used before v0.3 are kept as ``deprecated_aliases`` — they
resolve to the canonical slug and emit a ``DeprecationWarning``. Drop in v0.4.
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    per_unit,
    route_images,
)

# (canonical_slug, flat_usd_per_image, {deprecated_legacy_ids})
_IMAGE_MODELS: tuple[tuple[str, float, frozenset[str]], ...] = (
    ("seedream-5.0-lite", 0.035, frozenset({"Seedream-5.0-Lite"})),
    ("gemini-2.5-flash-image", 0.039, frozenset({"Gemini-2.5-Flash-Image"})),
    ("flux-kontext-pro", 0.05, frozenset({"FLUX-Kontext-Pro"})),
    ("seededit-3-0-i2i-250628", 0.03, frozenset({"Seededit"})),
    # Reve family — fast/normal, create/edit/remix variants.
    ("reve-create-20250915", 0.007, frozenset()),
    ("reve-edit-20250915", 0.007, frozenset()),
    ("reve-edit-fast-20251030", 0.007, frozenset({"Reve-Edit-Fast"})),
    ("reve-remix-20250915", 0.007, frozenset()),
    ("reve-remix-fast-20251030", 0.007, frozenset()),
    # Bria fibo family — the PascalCase names mapped here are the legacy ids
    # of three specific operations; the other fibo variants have no prior name.
    ("bria-fibo-image-blend", 0.02, frozenset({"Bria-Blending"})),
    ("bria-fibo-relight", 0.02, frozenset({"Bria-Relighting"})),
    ("bria-fibo-restore", 0.02, frozenset({"Bria-Restoration"})),
    ("bria-genfill", 0.02, frozenset()),
    ("bria-eraser", 0.02, frozenset()),
)

_COMMON_ALLOWLIST = frozenset(
    {"prompt", "negative_prompt", "seed", "aspect_ratio", "number_of_images", "image"}
)
_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


def _image_spec(model_id: str, flat_rate: float, deprecated: frozenset[str]) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.IMAGE,
        deprecated_aliases=deprecated,
        pricing=per_unit(flat_rate),
        param_allowlist=_COMMON_ALLOWLIST,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.IMAGE,
    input_mapping=_COMMON_INPUT,
    extras=_ENVELOPE,
)


def build_image_registry() -> ModelRegistry:
    defaults = {mid: _image_spec(mid, rate, dep) for mid, rate, dep in _IMAGE_MODELS}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
