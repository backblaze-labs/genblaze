"""Google model families — Veo (video) and Imagen (image).

Pattern-keyed; future ``veo-N`` / ``imagen-N`` slugs inherit param
shape automatically. Both families ship the
``google_models_get_probe`` so ``Pipeline.preflight()`` returns
``OK_AUTHORITATIVE`` when the slug exists on the user's project,
``NOT_FOUND`` when it doesn't, and ``OK_PROVISIONAL`` when google-genai
returns a non-404 error (auth, region, transport).

Pricing intentionally absent: see
``docs/reference/pricing-recipes.md`` for Veo (per-second by model)
and Imagen (per-image by model) recipes.
"""

from __future__ import annotations

import re
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.providers import (
    ModelFamily,
    ModelSpec,
)

from ._probe import google_models_get_probe

# --- Constraint helpers (constructor-cheap, family-shared) ----------------

_VEO_VALID_ASPECT_RATIOS = frozenset({"16:9", "9:16"})
_VEO_VALID_RESOLUTIONS = frozenset({"720p", "1080p", "4k"})
_VEO_VALID_DURATIONS = frozenset({"4", "6", "8"})
_IMAGEN_VALID_ASPECT_RATIOS = frozenset({"1:1", "3:4", "4:3", "9:16", "16:9"})


def _check_veo_aspect_ratio(params: dict[str, Any]) -> None:
    ar = params.get("aspect_ratio")
    if ar is not None and ar not in _VEO_VALID_ASPECT_RATIOS:
        raise ProviderError(
            f"Invalid aspect_ratio={ar!r}. Must be one of {set(_VEO_VALID_ASPECT_RATIOS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _check_veo_resolution(params: dict[str, Any]) -> None:
    res = params.get("resolution")
    if res is not None and res not in _VEO_VALID_RESOLUTIONS:
        raise ProviderError(
            f"Invalid resolution={res!r}. Must be one of {set(_VEO_VALID_RESOLUTIONS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _check_veo_duration(params: dict[str, Any]) -> None:
    if "duration_seconds" not in params:
        return
    dur = params["duration_seconds"]
    if dur not in _VEO_VALID_DURATIONS:
        raise ProviderError(
            f"Invalid duration_seconds={dur!r}. Must be one of {set(_VEO_VALID_DURATIONS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _check_imagen_aspect_ratio(params: dict[str, Any]) -> None:
    ar = params.get("aspect_ratio")
    if ar is not None and ar not in _IMAGEN_VALID_ASPECT_RATIOS:
        raise ProviderError(
            f"Invalid aspect_ratio={ar!r}. Must be one of {_IMAGEN_VALID_ASPECT_RATIOS}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


# --- Families -------------------------------------------------------------

# Shared param contract for all Veo variants — duration alias / coercer
# / constraints are identical across legacy and modern. Only ``extras``
# differs (audio capability).
_VEO_PARAM_CONTRACT: dict = {
    # Standard "duration" maps to Veo-native "duration_seconds".
    "param_aliases": {"duration": "duration_seconds"},
    # Veo expects "4"/"6"/"8" string form.
    "param_coercers": {"duration_seconds": str},
    "param_constraints": (
        _check_veo_aspect_ratio,
        _check_veo_resolution,
        _check_veo_duration,
    ),
}


# Veo 2 — text-to-video only, NO synchronized audio. Pattern is tightly
# bound to ``^veo-2[.-]`` so ``veo-2.0-generate-001`` and a hypothetical
# ``veo-2-pro`` both match here, but ``veo-3.x``, ``veo-4.x``, and any
# future major versions fall through to ``GOOGLE_VEO_FAMILY``.
#
# Order matters: this family is listed FIRST in
# ``provider.py::create_registry`` so the legacy pattern wins on
# first-match for every veo-2 slug. The catch-all ``^veo-`` family
# only applies to slugs that didn't match here.
GOOGLE_VEO_LEGACY_FAMILY = ModelFamily(
    name="google-veo-legacy",
    # ``[.-]`` matches the standard separator after the major version
    # (``veo-2.0-...`` / ``veo-2-pro``). The trailing ``$`` accepts the
    # bare ``veo-2`` slug too — without it, a programmatic lookup of
    # the bare slug would fall through to the modern catch-all and
    # silently inherit ``has_audio=True``.
    pattern=re.compile(r"^veo-2(?:[.-]|$)"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        **_VEO_PARAM_CONTRACT,
    ),
    description=(
        "Google Veo 2 — text-to-video generation. Does not render "
        "synchronized audio (use Veo 3+ via ``GOOGLE_VEO_FAMILY``)."
    ),
    example_slugs=("veo-2.0-generate-001",),
    probe=google_models_get_probe,
)


# Veo 3+ — catch-all for non-legacy ``^veo-`` slugs. Includes
# synchronized audio. ``extras["has_audio"]`` is the typed signal the
# provider reads to populate ``VideoMetadata.has_audio`` and the
# multi-track asset metadata (replaces the legacy
# ``step.model.startswith("veo-3")`` string check).
GOOGLE_VEO_FAMILY = ModelFamily(
    name="google-veo",
    pattern=re.compile(r"^veo-"),  # catches everything not legacy
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        extras={"has_audio": True},
        **_VEO_PARAM_CONTRACT,
    ),
    description=(
        "Google Veo 3+ — text/image-to-video generation with "
        "synchronized audio. Catches every ``veo-`` slug not matched "
        "by ``GOOGLE_VEO_LEGACY_FAMILY``."
    ),
    example_slugs=(
        "veo-3.0-generate-001",
        "veo-3.0-fast-generate-001",
    ),
    probe=google_models_get_probe,
)


GOOGLE_IMAGEN_FAMILY = ModelFamily(
    name="google-imagen",
    pattern=re.compile(r"^imagen-"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.IMAGE,
        param_constraints=(_check_imagen_aspect_ratio,),
    ),
    description="Google Imagen family — text-to-image generation.",
    # imagen-3.0-* left the catalog (404 on models.get -> DEAD) and were
    # replaced by imagen-4.0-*, which IS catalog-listed but entitlement-gated
    # for new keys. The plain catalog-membership probe can't see that gate
    # (models.get returns 200); ImagenProvider.validate_model re-grades the
    # known-gated imagen-4.0-* slugs to OK_PROVISIONAL rather than a billable
    # :predict probe or a hard DEAD — see issue #206.
    example_slugs=(
        "imagen-4.0-generate-001",
        "imagen-4.0-fast-generate-001",
    ),
    probe=google_models_get_probe,
)


# Gemini-native image models — a DIFFERENT wire shape from Imagen
# (generateContent + inline base64 bytes, not :predict). The only image
# path callable on a freshly created Gemini API key, since imagen-4.0-*
# is entitlement-gated there (issue #205). ``.*`` before the required
# "image" segment excludes chat models (gemini-2.5-flash) while still
# matching the "-preview" suffixed slugs (gemini-3-pro-image-preview) —
# anchoring with a trailing ``$`` would miss those.
GOOGLE_GEMINI_IMAGE_FAMILY = ModelFamily(
    name="google-gemini-image",
    pattern=re.compile(r"^gemini-.*-image"),
    spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
    description=(
        "Gemini-native image generation via generateContent — inline "
        "base64 bytes, not Imagen's :predict shape."
    ),
    example_slugs=(
        "gemini-2.5-flash-image",
        "gemini-3.1-flash-image",
    ),
    # No reported entitlement gap for this family (issue #205) — plain
    # catalog-membership probe is sufficient.
    probe=google_models_get_probe,
)


__all__ = [
    "GOOGLE_GEMINI_IMAGE_FAMILY",
    "GOOGLE_IMAGEN_FAMILY",
    "GOOGLE_VEO_FAMILY",
    "GOOGLE_VEO_LEGACY_FAMILY",
]
