"""GMICloud audio-model families (TTS, voice-clone, music).

Three families distinguished by upstream payload contract:

* ``gmi-audio-tts`` — text-to-speech models (ElevenLabs, MiniMax-TTS,
  Inworld). Standard audio surface with ``voice``→``voice_id`` aliasing.
* ``gmi-audio-clone`` — voice-clone models (MiniMax-Voice-Clone). Adds
  expressive controls (pitch, emotion, speed, stability, similarity)
  and routes chain-input audio to ``reference_audio``.
* ``gmi-audio-music`` — music generation (MiniMax-Music). Adds
  style_weight, duration_seconds, tempo. Stereo output via
  ``extras["is_music"]=True``.

Every audio slug currently shipped by GMI was flagged ``suspected_dead``
in the 2026-04 reconciliation — those slugs are preserved in each
family's ``unstable_examples`` so users see a "known unstable" hint at
preflight (per RT-10 of the catalog-decoupling plan). The empty-payload
probe is the authoritative answer at runtime.
"""

from __future__ import annotations

import re

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ParamSurface,
    route_audio,
)

from .._probe import empty_payload_request_probe

# Default audio surface — universally meaningful audio params plus the
# canonical ``voice``→``voice_id`` rename most callers hit.
_AUDIO_BASE = ParamSurface.for_modality(Modality.AUDIO).with_aliases(voice="voice_id")

# Voice clone variants accept a reference audio plus expressive controls.
_VOICE_CLONE = _AUDIO_BASE.extend("pitch", "emotion", "speed", "stability", "similarity")

# Music models accept style hints + per-second duration controls.
_MUSIC = _AUDIO_BASE.extend("style_weight", "duration_seconds", "tempo")


_ENVELOPE = {"envelope_key": "payload"}
_CLONE_INPUT = route_audio(slot="reference_audio")


# GMICloud's published catalog (2026-03-10 "Most Popular AI Models" blog)
# lists every audio slug in lowercase: ``elevenlabs-tts-v3``,
# ``minimax-tts-speech-2.6-turbo``, ``minimax-audio-voice-clone-speech-2.6-hd``,
# ``minimax-music-2.5``, ``inworld-tts-1.5-mini``. Patterns are case-
# insensitive so pre-0.3.2 PascalCase callers continue to match the right
# family, and ``canonical_slug=str.lower`` rewrites the wire form to what
# GMI accepts. The rewrite emits a one-time INFO per (family, input) so
# callers know to migrate their call sites.


def _voice_clone_canonical(slug: str) -> str:
    """Rewrite voice-clone slugs to GMI's published canonical form.

    GMI's 2026-03-10 catalog ships the slug as
    ``minimax-audio-voice-clone-speech-2.6-hd`` — note the ``-audio-``
    segment. Pre-0.3.2 connector code (and any pre-existing user code
    following the older convention) used ``MiniMax-Voice-Clone-Speech-2.6-HD``
    (no ``-Audio-``); plain ``str.lower`` produces
    ``minimax-voice-clone-speech-2.6-hd`` which doesn't match GMI's
    catalog. This canonical_slug inserts the missing ``-audio-`` segment
    so legacy and current call sites both produce the right wire form.
    """
    low = slug.lower()
    legacy_prefix = "minimax-voice-clone"
    if low.startswith(legacy_prefix) and not low.startswith("minimax-audio-voice-clone"):
        return "minimax-audio-voice-clone" + low[len(legacy_prefix) :]
    return low


_GMI_AUDIO_CLONE_FAMILY = ModelFamily(
    name="gmi-audio-clone",
    # Match both the legacy ``minimax-voice-clone-*`` shape and GMI's
    # current canonical ``minimax-audio-voice-clone-*`` shape (with the
    # ``-audio-`` segment) — the canonical_slug rewrites the former to
    # the latter on the wire.
    pattern=re.compile(r"^minimax-(?:audio-)?voice-clone", re.IGNORECASE),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        input_mapping=_CLONE_INPUT,
        extras={**_ENVELOPE, "is_music": False},
        **_VOICE_CLONE.build(),
    ),
    description="GMICloud voice-clone TTS (MiniMax Voice-Clone family).",
    example_slugs=("minimax-audio-voice-clone-speech-2.6-hd",),
    canonical_slug=_voice_clone_canonical,
    probe=empty_payload_request_probe,
)

_GMI_AUDIO_MUSIC_FAMILY = ModelFamily(
    name="gmi-audio-music",
    pattern=re.compile(r"^minimax-music", re.IGNORECASE),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        extras={**_ENVELOPE, "is_music": True},
        **_MUSIC.build(),
    ),
    description="GMICloud music generation (MiniMax Music family).",
    example_slugs=("minimax-music-2.5",),
    canonical_slug=str.lower,
    probe=empty_payload_request_probe,
)

_GMI_AUDIO_TTS_FAMILY = ModelFamily(
    name="gmi-audio-tts",
    # ElevenLabs / MiniMax-TTS / Inworld TTS variants. Voice-Clone and
    # Music get their own families above (more-specific patterns checked
    # first per the family-resolution contract).
    pattern=re.compile(r"^(?:elevenlabs-tts|minimax-tts|inworld-tts)", re.IGNORECASE),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        extras={**_ENVELOPE, "is_music": False},
        **_AUDIO_BASE.build(),
    ),
    description="GMICloud text-to-speech (ElevenLabs, MiniMax-TTS, Inworld families).",
    example_slugs=(
        "elevenlabs-tts-v3",
        "minimax-tts-speech-2.6-turbo",
        "inworld-tts-1.5-mini",
    ),
    canonical_slug=str.lower,
    probe=empty_payload_request_probe,
)


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    param_aliases={"voice": "voice_id"},
    # ``is_music: False`` is documented on the family-level templates;
    # carry the same default on the fallback so a slug that misses every
    # family (e.g., a brand-new vendor namespace) still gets the
    # mono-audio assumption explicitly rather than reading ``None`` from
    # ``extras.get("is_music")`` and accidentally turning truthy via a
    # future ``if extras["is_music"]:`` check.
    extras={**_ENVELOPE, "is_music": False},
)


def build_audio_registry() -> ModelRegistry:
    """Return the default audio ``ModelRegistry`` — pattern-keyed."""
    return ModelRegistry(
        # Order matters: more-specific patterns first.
        provider_families=(
            _GMI_AUDIO_CLONE_FAMILY,
            _GMI_AUDIO_MUSIC_FAMILY,
            _GMI_AUDIO_TTS_FAMILY,
        ),
        fallback=_FALLBACK,
    )
