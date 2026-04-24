"""NVIDIA video-model specs (Cosmos, Edify Video).

Model slugs follow build.nvidia.com's ``vendor/slug`` convention. The endpoint
path is derived at request time as ``/genai/{model_id}`` — nothing model-
specific lives in code.

Cosmos endpoints are still enterprise-gated as of 2026-04; the provider still
works (the code path is correct) but callers on the free tier will see
AUTH_FAILURE responses until they have access. Unlisted models pass through
the permissive fallback spec — no code change needed when NVIDIA ships new
video models.
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    route_images,
    route_video,
)

# Curated list — kept narrow on purpose. Unlisted models get the fallback.
_VIDEO_MODELS: tuple[str, ...] = (
    "nvidia/cosmos-1.0-7b-diffusion-text2world",
    "nvidia/cosmos-1.0-7b-diffusion-video2world",
    "nvidia/cosmos-2.0-diffusion-text2world",
    "nvidia/cosmos-2.0-diffusion-video2world",
)

# Canonical → native renames. NVIDIA generation endpoints accept either name
# on the wire, but keeping the alias explicit makes manifests more readable
# and lets genblaze users write in the canonical form everywhere.
_COMMON_ALIASES = {"guidance_scale": "cfg_scale"}

# Route chain inputs: most models take an image (image-to-video); video2world
# variants take a video instead. ``text2world`` / text-only models simply
# don't read either slot — the payload keys just aren't present.
_IMAGE_INPUT = route_images(slots=("image",))
_VIDEO_INPUT = route_video(slot="video")


def _video_spec(model_id: str) -> ModelSpec:
    """Build a spec for a curated video model.

    Pricing intentionally ``None``: NVIDIA's free tier is RPM-gated with no
    per-token billing, and Cosmos enterprise pricing is contract-specific.
    Users can attach pricing at runtime via ``registry.register_pricing``.
    """
    input_mapping = _VIDEO_INPUT if "video2world" in model_id else _IMAGE_INPUT
    return ModelSpec(
        model_id=model_id,
        modality=Modality.VIDEO,
        pricing=None,
        param_aliases=_COMMON_ALIASES,
        input_mapping=input_mapping,
    )


# Fallback spec: permissive for unlisted models, but keeps the image-input
# routing so e.g. a new image-to-video model just works out of the box.
_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_aliases=_COMMON_ALIASES,
    input_mapping=_IMAGE_INPUT,
)


def build_video_registry() -> ModelRegistry:
    """Return the default video ModelRegistry."""
    return ModelRegistry(
        defaults={mid: _video_spec(mid) for mid in _VIDEO_MODELS},
        fallback=_FALLBACK,
    )
