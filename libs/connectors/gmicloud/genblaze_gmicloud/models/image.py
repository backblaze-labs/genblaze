"""GMICloud image-model families.

Two specialized families plus the permissive fallback:

* ``gmi-image-bria-inpaint`` — Bria genfill / eraser. Adds ``mask`` /
  ``mask_url`` / ``denoise`` / ``strength`` to the allowlist so the
  inpainting payload isn't stripped.
* ``gmi-image-edit`` — Seededit / Reve edit / Reve remix variants. Adds
  ``image_url`` / ``strength``.

All other GMI image slugs (Seedream, Gemini-Flash, FLUX-Kontext, Reve
create, Bria fibo blend/relight/restore, etc.) hit the permissive
fallback with the standard image surface, which is what they need.

The empty-payload probe surfaces dead slugs at preflight.
"""

from __future__ import annotations

import re

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ParamSurface,
    route_images,
)

from .._probe import empty_payload_request_probe

# Default image surface — the universally meaningful image params plus the
# GMI request-queue ``number_of_images`` control.
_IMAGE_BASE = ParamSurface.for_modality(Modality.IMAGE).extend("number_of_images")

# Bria inpaint models need a mask + denoise/strength surface.
_BRIA_INPAINT = _IMAGE_BASE.extend("mask", "mask_url", "denoise", "strength")

# Reve edit / remix variants accept a strength + edit-specific reference image.
_REVE_EDIT = _IMAGE_BASE.extend("image_url", "strength")


_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


_GMI_IMAGE_BRIA_INPAINT_FAMILY = ModelFamily(
    name="gmi-image-bria-inpaint",
    pattern=re.compile(r"^bria-(?:genfill|eraser)$"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.IMAGE,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
        **_BRIA_INPAINT.build(),
    ),
    description="Bria inpainting (genfill / eraser) — needs mask payload.",
    example_slugs=("bria-genfill", "bria-eraser"),
    probe=empty_payload_request_probe,
)

_GMI_IMAGE_EDIT_FAMILY = ModelFamily(
    name="gmi-image-edit",
    # Seededit and Reve edit/remix variants. Bria fibo (blend/relight/
    # restore) and Reve create use the base surface — they don't match
    # this pattern.
    pattern=re.compile(r"^(?:seededit-|reve-edit|reve-remix)"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.IMAGE,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
        **_REVE_EDIT.build(),
    ),
    description="Image-to-image edit/remix variants (Seededit, Reve edit/remix).",
    example_slugs=(
        "seededit-3-0-i2i-250628",
        "reve-edit-20250915",
        "reve-edit-fast-20251030",
        "reve-remix-20250915",
        "reve-remix-fast-20251030",
    ),
    probe=empty_payload_request_probe,
)


# Permissive fallback covers the rest — Seedream, Gemini-Flash,
# FLUX-Kontext, Reve create, Bria fibo blend/relight/restore. The
# default image surface is permissive enough to pass their payloads
# through unchanged.
_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.IMAGE,
    input_mapping=_COMMON_INPUT,
    extras=_ENVELOPE,
)


def build_image_registry() -> ModelRegistry:
    """Return the default image ``ModelRegistry`` — pattern-keyed."""
    return ModelRegistry(
        provider_families=(
            _GMI_IMAGE_BRIA_INPAINT_FAMILY,
            _GMI_IMAGE_EDIT_FAMILY,
        ),
        fallback=_FALLBACK,
    )
