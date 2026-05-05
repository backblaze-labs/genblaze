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

_GMI_VIDEO_VEO_FAMILY = ModelFamily(
    name="gmi-video-veo",
    # ``^veo\d+`` (no end anchor) absorbs future Google Veo variants
    # — ``-fast``, ``-pro``, ``-ultra``, ``-2025``, etc. The
    # has_audio property is intrinsic to the model family, not the
    # variant tier, so all variants inherit it correctly.
    pattern=re.compile(r"^veo\d+"),
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
    example_slugs=("veo3",),
    unstable_examples=("veo3-fast",),
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
_UNSTABLE_SLUGS: frozenset[str] = frozenset(
    {
        "kling-text2video-v2.1-master",
        "minimax-hailuo-2.3-fast",
        "vidu-q1",
    }
)


def build_video_registry() -> ModelRegistry:
    """Return the default video ``ModelRegistry`` — pattern-keyed."""
    return ModelRegistry(
        provider_families=(
            _GMI_VIDEO_PIXVERSE_FAMILY,
            _GMI_VIDEO_WAN_R2V_FAMILY,
            _GMI_VIDEO_VEO_FAMILY,
        ),
        fallback=_FALLBACK,
        unstable_slugs=_UNSTABLE_SLUGS,
    )
