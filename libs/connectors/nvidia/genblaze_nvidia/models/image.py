"""NVIDIA image-model families (SDXL, SD 3.5, FLUX).

Three families, ordered most-specific-first per the family-resolution
contract:

1. ``nvidia-image-sdxl`` — Stable Diffusion XL. Needs a payload
   transformer that wraps ``prompt`` / ``negative_prompt`` into NIM's
   legacy ``text_prompts`` array.
2. ``nvidia-image-sd3`` — Stable Diffusion 3.x. Modern ``prompt`` field.
3. ``nvidia-image-flux`` — Black Forest Labs FLUX.1 family.

The empty-payload genai probe surfaces dead slugs at preflight, same as
audio/video.
"""

from __future__ import annotations

import re

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    route_images,
)

from .._probe import empty_payload_genai_probe

# Canonical → native rename most callers hit.
_COMMON_ALIASES = {"guidance_scale": "cfg_scale"}

# Image-to-image flows route the first image asset to ``image``.
_COMMON_INPUT = route_images(slots=("image",))


def _sdxl_transformer(params: dict) -> dict:
    """SDXL-specific: wrap ``prompt`` / ``negative_prompt`` in ``text_prompts``.

    SDXL on NIM takes ``text_prompts=[{"text": ..., "weight": ...}]`` instead
    of the newer ``prompt`` field. Convert so users can write prompts in the
    canonical form and still hit SDXL.
    """
    out = dict(params)
    prompt = out.pop("prompt", None)
    negative = out.pop("negative_prompt", None)
    if prompt is not None or negative is not None:
        text_prompts: list[dict] = []
        if prompt is not None:
            text_prompts.append({"text": prompt, "weight": 1.0})
        if negative is not None:
            text_prompts.append({"text": negative, "weight": -1.0})
        # Don't clobber a user-supplied text_prompts — they know what they want.
        out.setdefault("text_prompts", text_prompts)
    return out


_NVIDIA_SDXL_FAMILY = ModelFamily(
    name="nvidia-image-sdxl",
    # SDXL pattern must be checked before SD3 — both live under the
    # ``stabilityai/`` namespace, but SDXL's payload shape is different.
    pattern=re.compile(r"^stabilityai/stable-diffusion-xl"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.IMAGE,
        param_aliases=_COMMON_ALIASES,
        param_transformer=_sdxl_transformer,
        input_mapping=_COMMON_INPUT,
    ),
    description="Stability AI Stable Diffusion XL on NIM (text_prompts payload).",
    example_slugs=("stabilityai/stable-diffusion-xl",),
    probe=empty_payload_genai_probe,
)

_NVIDIA_SD3_FAMILY = ModelFamily(
    name="nvidia-image-sd3",
    pattern=re.compile(r"^stabilityai/stable-diffusion-3"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.IMAGE,
        param_aliases=_COMMON_ALIASES,
        input_mapping=_COMMON_INPUT,
    ),
    description="Stability AI Stable Diffusion 3.x family on NIM.",
    example_slugs=(
        "stabilityai/stable-diffusion-3-5-large",
        "stabilityai/stable-diffusion-3-5-large-turbo",
        "stabilityai/stable-diffusion-3-5-medium",
    ),
    probe=empty_payload_genai_probe,
)

_NVIDIA_FLUX_FAMILY = ModelFamily(
    name="nvidia-image-flux",
    pattern=re.compile(r"^black-forest-labs/flux\."),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.IMAGE,
        param_aliases=_COMMON_ALIASES,
        input_mapping=_COMMON_INPUT,
    ),
    description="Black Forest Labs FLUX.1 family on NIM.",
    example_slugs=(
        "black-forest-labs/flux.1-schnell",
        "black-forest-labs/flux.1-dev",
    ),
    probe=empty_payload_genai_probe,
)


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.IMAGE,
    param_aliases=_COMMON_ALIASES,
    input_mapping=_COMMON_INPUT,
)


def build_image_registry() -> ModelRegistry:
    """Return the default image ``ModelRegistry`` — pattern-keyed."""
    return ModelRegistry(
        provider_families=(
            _NVIDIA_SDXL_FAMILY,
            _NVIDIA_SD3_FAMILY,
            _NVIDIA_FLUX_FAMILY,
        ),
        fallback=_FALLBACK,
    )
