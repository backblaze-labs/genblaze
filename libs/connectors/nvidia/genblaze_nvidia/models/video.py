"""NVIDIA video-model families (Cosmos text2world / video2world).

Two families distinguished by chain-input shape:

* ``nvidia-cosmos-text2world`` — image-to-video (chain inputs route via
  ``image``).
* ``nvidia-cosmos-video2world`` — video-to-video (chain inputs route via
  ``video``).

The pattern absorbs any future Cosmos minor/major version (1.0, 2.0, ...)
without code changes — the SDK ships the param shape, not the slug list.
``nvidia/cosmos-*-text2world`` and ``nvidia/cosmos-*-video2world`` slugs
inherit the right family automatically.

The empty-payload genai probe surfaces dead slugs at preflight. Cosmos
is enterprise-gated on the free tier — probes against unauthorized
endpoints return 401/403, which the probe maps to ``UNKNOWN`` (we can't
say "dead" without auth). That's the honest answer.
"""

from __future__ import annotations

import re

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    route_images,
    route_video,
)

from .._probe import empty_payload_genai_probe

# Canonical → native param renames. NVIDIA accepts either name on the wire,
# but keeping the alias explicit makes manifests more readable and lets
# genblaze users write canonical-form prompts everywhere.
_COMMON_ALIASES = {"guidance_scale": "cfg_scale"}

_IMAGE_INPUT = route_images(slots=("image",))
_VIDEO_INPUT = route_video(slot="video")


_NVIDIA_COSMOS_TEXT2WORLD_FAMILY = ModelFamily(
    name="nvidia-cosmos-text2world",
    pattern=re.compile(r"^nvidia/cosmos-.*-text2world"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_aliases=_COMMON_ALIASES,
        input_mapping=_IMAGE_INPUT,
    ),
    description="NVIDIA Cosmos text-to-video family (image-conditioned).",
    example_slugs=(
        "nvidia/cosmos-1.0-7b-diffusion-text2world",
        "nvidia/cosmos-2.0-diffusion-text2world",
    ),
    probe=empty_payload_genai_probe,
)

_NVIDIA_COSMOS_VIDEO2WORLD_FAMILY = ModelFamily(
    name="nvidia-cosmos-video2world",
    pattern=re.compile(r"^nvidia/cosmos-.*-video2world"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_aliases=_COMMON_ALIASES,
        input_mapping=_VIDEO_INPUT,
    ),
    description="NVIDIA Cosmos video-to-video family.",
    example_slugs=(
        "nvidia/cosmos-1.0-7b-diffusion-video2world",
        "nvidia/cosmos-2.0-diffusion-video2world",
    ),
    probe=empty_payload_genai_probe,
)


# Permissive fallback: keeps the image-input routing so an unrecognized
# image-to-video slug still works out of the box.
_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_aliases=_COMMON_ALIASES,
    input_mapping=_IMAGE_INPUT,
)


def build_video_registry() -> ModelRegistry:
    """Return the default video ``ModelRegistry`` — pattern-keyed."""
    return ModelRegistry(
        provider_families=(
            _NVIDIA_COSMOS_TEXT2WORLD_FAMILY,
            _NVIDIA_COSMOS_VIDEO2WORLD_FAMILY,
        ),
        fallback=_FALLBACK,
    )
