"""GMICloud image-model specs."""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    per_unit,
    route_images,
)

_IMAGE_FLAT: dict[str, float] = {
    "Seedream-5.0-Lite": 0.035,
    "Gemini-2.5-Flash-Image": 0.039,
    "Reve-Edit-Fast": 0.007,
    "FLUX-Kontext-Pro": 0.05,
    "Seededit": 0.03,
    "Bria-Blending": 0.02,
    "Bria-Relighting": 0.02,
    "Bria-Restoration": 0.02,
}

_COMMON_ALLOWLIST = frozenset(
    {"prompt", "negative_prompt", "seed", "aspect_ratio", "number_of_images", "image"}
)
_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


def _image_spec(model_id: str, flat_rate: float) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.IMAGE,
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
    defaults = {mid: _image_spec(mid, rate) for mid, rate in _IMAGE_FLAT.items()}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
