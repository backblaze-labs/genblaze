"""NVIDIA image-model specs (SDXL, SD 3.5, FLUX).

Model slugs follow build.nvidia.com's ``vendor/slug`` convention. Pricing is
``None`` for all entries: the free tier is RPM-gated (no per-image billing)
and NVIDIA doesn't publish a stable public per-image rate. Users can attach
pricing at runtime. Unlisted models pass through the fallback spec.
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    route_images,
)

# SDXL uses ``text_prompts`` array + ``cfg_scale`` + ``sampler`` + ``steps``
# + ``width``/``height`` + ``seed``. SD 3.5 & FLUX use ``prompt`` + ``cfg_scale``
# + ``aspect_ratio`` + ``seed`` + ``steps``. The common alias covers the one
# rename most callers hit.
_COMMON_ALIASES = {"guidance_scale": "cfg_scale"}

# Chain inputs — image-to-image flows route the first image to ``image``.
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


_IMAGE_MODELS_PROMPT_STYLE: tuple[str, ...] = (
    "stabilityai/stable-diffusion-3-5-large",
    "stabilityai/stable-diffusion-3-5-large-turbo",
    "stabilityai/stable-diffusion-3-5-medium",
    "black-forest-labs/flux.1-schnell",
    "black-forest-labs/flux.1-dev",
)

_IMAGE_MODELS_SDXL_STYLE: tuple[str, ...] = ("stabilityai/stable-diffusion-xl",)


def _prompt_style_spec(model_id: str) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.IMAGE,
        pricing=None,
        param_aliases=_COMMON_ALIASES,
        input_mapping=_COMMON_INPUT,
    )


def _sdxl_style_spec(model_id: str) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.IMAGE,
        pricing=None,
        param_aliases=_COMMON_ALIASES,
        param_transformer=_sdxl_transformer,
        input_mapping=_COMMON_INPUT,
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.IMAGE,
    param_aliases=_COMMON_ALIASES,
    input_mapping=_COMMON_INPUT,
)


def build_image_registry() -> ModelRegistry:
    """Return the default image ModelRegistry."""
    defaults: dict[str, ModelSpec] = {}
    for mid in _IMAGE_MODELS_PROMPT_STYLE:
        defaults[mid] = _prompt_style_spec(mid)
    for mid in _IMAGE_MODELS_SDXL_STYLE:
        defaults[mid] = _sdxl_style_spec(mid)
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
