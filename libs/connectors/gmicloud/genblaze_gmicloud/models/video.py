"""GMICloud video-model families.

Three families, ordered most-specific-first:

1. ``gmi-video-pixverse`` — Pixverse v5.6 t2v / i2v / transition. Adds
   ``quality`` to the allowlist (required by the upstream API but
   stripped by the base surface).
2. ``gmi-video-wan-r2v`` — Wan reference-to-video variants. Adds
   ``image_url`` / ``tail_image_url`` for keyframe references. Pattern
   absorbs any future Wan major version.
3. ``gmi-video-veo`` — Google Veo family. Carries ``extras["has_audio"]
   = True`` so ``fetch_output`` knows to attach audio metadata to the
   asset alongside the video track. Carries ``veo3-fast`` as a known
   unstable example.

Slugs that don't match any family fall through to the permissive
fallback. Registry-level ``unstable_slugs`` carries the remaining
"known unstable" set (Kling v2.1-master, Minimax Hailuo, Vidu Q1) so
preflight surfaces a hint without requiring a spurious catch-all
family — the original "catch-all family carrying unstable_examples"
shape was a code smell that the registry-level field eliminates.

The 2026-04 reconciliation flagged four GMI video slugs as
``suspected_dead`` (404 against ``/requests``): ``veo3-fast``,
``kling-text2video-v2.1-master``, ``minimax-hailuo-2.3-fast``,
``vidu-q1``. The empty-payload probe is the authoritative answer at
runtime; preflight surfaces ``OK_PROVISIONAL`` with
``known_unstable`` detail until the probe confirms.
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

# Default video surface — universally meaningful video params + GMI's
# canonical-to-native ``guidance_scale``→``cfg_scale`` rename and
# duration coercion.
_VIDEO_BASE = (
    ParamSurface.for_modality(Modality.VIDEO)
    .with_aliases(guidance_scale="cfg_scale")
    .with_coercers(duration=int)
    .extend("cfg_scale")
)

# Pixverse models require ``quality`` per the upstream API.
_PIXVERSE = _VIDEO_BASE.extend("quality")

# Wan transition / r2v variants accept multiple keyframes via image_url.
_WAN_REF = _VIDEO_BASE.extend("image_url", "tail_image_url")


_COMMON_INPUT = route_images(slots=("image",))
_ENVELOPE = {"envelope_key": "payload"}


_GMI_VIDEO_PIXVERSE_FAMILY = ModelFamily(
    name="gmi-video-pixverse",
    pattern=re.compile(r"^pixverse-"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
        **_PIXVERSE.build(),
    ),
    description="Pixverse v5.6 family — t2v, i2v, transition.",
    example_slugs=(
        "pixverse-v5.6-t2v",
        "pixverse-v5.6-i2v",
        "pixverse-v5.6-transition",
    ),
    probe=empty_payload_request_probe,
)

_GMI_VIDEO_WAN_R2V_FAMILY = ModelFamily(
    name="gmi-video-wan-r2v",
    # ``\d+`` for major-version digits absorbs Wan 3.x and beyond
    # without a code change — addresses the "over-constrained version
    # anchor" red-team finding.
    pattern=re.compile(r"^wan\d+\.\d+-r2v$"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
        **_WAN_REF.build(),
    ),
    description="Wan reference-to-video — keyframe-conditioned generation.",
    example_slugs=("wan2.6-r2v",),
    probe=empty_payload_request_probe,
)


def _veo_canonical(slug: str) -> str:
    """Map any case of ``veo<digits>...`` to GMI's published PascalCase form.

    GMI's catalog uses ``Veo3``, ``Veo3-Fast``, ``Veo3-Pro``, etc. — the
    ``Veo`` prefix is always capitalized, the rest of the suffix
    preserves its original casing. Accepts lowercase ``veo3``,
    SCREAMING_CASE, and the PascalCase canonical form; emits the
    canonical form for the wire.

    The Veo family pattern ``^veo\\d+`` (case-insensitive) requires at
    least 4 characters (``veo`` + one digit), so ``slug[3:]`` is always
    safe here — the pattern is the invariant, no length guard needed.
    """
    return "Veo" + slug[3:]


_GMI_VIDEO_VEO_FAMILY = ModelFamily(
    name="gmi-video-veo",
    # Case-insensitive ``^veo\d+`` absorbs lowercase ``veo3`` (pre-0.3.2
    # convention), PascalCase ``Veo3`` (GMI's canonical wire form per
    # every 2025-12-08 → 2026-04-14 blog), and future variants like
    # ``-fast`` / ``-pro``. ``canonical_slug`` rewrites the wire form to
    # PascalCase; the rewrite emits a one-time INFO so callers know to
    # migrate their call sites.
    pattern=re.compile(r"^veo\d+", re.IGNORECASE),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        input_mapping=_COMMON_INPUT,
        # ``has_audio`` lives on the family spec rather than as a
        # separate frozenset in the provider module — keeps the
        # "this model produces audio" signal alongside the family
        # definition where future maintainers will look for it.
        extras={**_ENVELOPE, "has_audio": True},
        **_VIDEO_BASE.build(),
    ),
    description="Google Veo family on GMI — produces video + audio tracks.",
    example_slugs=("Veo3", "Veo3-Fast"),
    canonical_slug=_veo_canonical,
    probe=empty_payload_request_probe,
)


# Kling V2.1 wire form is PascalCase per GMI's 2026-04-14 blog
# (``Kling-Text2Video-V2.1-Master`` $0.28/req, ``Kling-Image2Video-V2.1-Master``
# $0.28/req). Newer V2.5/V3 series ship lowercase (``kling-v2-5-turbo``,
# ``kling-v3-text-to-video``); they hit the permissive fallback. This
# family captures only the PascalCase V2.1 family that needs canonical
# rewriting from lowercase callers.
_KLING_V21_CANONICAL: dict[str, str] = {
    "kling-text2video-v2.1-master": "Kling-Text2Video-V2.1-Master",
    "kling-image2video-v2.1-master": "Kling-Image2Video-V2.1-Master",
}


def _kling_v21_canonical(slug: str) -> str:
    """Map lowercase ``kling-text2video-v2.1-master`` → the PascalCase wire
    form per GMI's 2026-04-14 catalog blog (``Kling-Text2Video-V2.1-Master``,
    ``Kling-Image2Video-V2.1-Master``).

    Embedded mixed-case (``Text2Video``, with both T AND V capitalized)
    defeats a simple per-segment Title-Case heuristic, so the mapping is
    spelled out explicitly. Only two Kling V2.1 slugs exist; the map
    stays trivial to maintain. Unmapped inputs (e.g. PascalCase callers
    already using the canonical form) round-trip unchanged.
    """
    return _KLING_V21_CANONICAL.get(slug.lower(), slug)


_GMI_VIDEO_KLING_V21_FAMILY = ModelFamily(
    name="gmi-video-kling-v21",
    pattern=re.compile(r"^kling-(?:text2video|image2video)-v2\.1-master$", re.IGNORECASE),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        input_mapping=_COMMON_INPUT,
        extras=_ENVELOPE,
        **_VIDEO_BASE.build(),
    ),
    description="Kling V2.1 (Master) — Text2Video / Image2Video on GMI.",
    example_slugs=("Kling-Text2Video-V2.1-Master", "Kling-Image2Video-V2.1-Master"),
    canonical_slug=_kling_v21_canonical,
    probe=empty_payload_request_probe,
)


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_aliases={"guidance_scale": "cfg_scale"},
    param_coercers={"duration": int},
    input_mapping=_COMMON_INPUT,
    extras=_ENVELOPE,
)


# Registry-level unstable slugs — known-unstable upstream but no
# dedicated family ships specialized param shape for them. Replaces the
# old "catch-all family carrying unstable_examples" pattern (a code
# smell where the family was a no-op except for the unstable list).
# ``veo3-fast`` is intentionally NOT here because it lives in the Veo
# family's own ``unstable_examples`` (the union happens at registry
# construction).
# Pre-0.3.2 ``_UNSTABLE_SLUGS`` carried lowercase variants of slugs whose
# canonical wire form is PascalCase (``kling-text2video-v2.1-master``,
# ``minimax-hailuo-2.3-fast``). With the new ``canonical_slug`` rewrite on
# the Kling V2.1 family, those lowercase forms now resolve to the right
# wire ids — they're not unstable, just non-canonical user input.
# ``vidu-q1`` was already removed from GMI's catalog (replaced by
# ``vidu-q3-pro-i2v`` per the 2026-03-04 blog); it stays flagged here
# until a maintainer confirms via the probe tool.
_UNSTABLE_SLUGS: frozenset[str] = frozenset({"vidu-q1"})


def build_video_registry() -> ModelRegistry:
    """Return the default video ``ModelRegistry`` — pattern-keyed."""
    return ModelRegistry(
        provider_families=(
            _GMI_VIDEO_PIXVERSE_FAMILY,
            _GMI_VIDEO_WAN_R2V_FAMILY,
            _GMI_VIDEO_VEO_FAMILY,
            _GMI_VIDEO_KLING_V21_FAMILY,
        ),
        fallback=_FALLBACK,
        unstable_slugs=_UNSTABLE_SLUGS,
    )
