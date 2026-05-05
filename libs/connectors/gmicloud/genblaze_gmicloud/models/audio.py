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


_GMI_AUDIO_CLONE_FAMILY = ModelFamily(
    name="gmi-audio-clone",
    pattern=re.compile(r"^MiniMax-Voice-Clone"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        input_mapping=_CLONE_INPUT,
        extras={**_ENVELOPE, "is_music": False},
        **_VOICE_CLONE.build(),
    ),
    description="GMICloud voice-clone TTS (MiniMax Voice-Clone family).",
    example_slugs=("MiniMax-Voice-Clone-Speech-2.6-HD",),
    # 2026-04 reconciliation: all GMI audio slugs returned 404 against the
    # live /requests endpoint. Flagged as known-unstable until the probe
    # confirms or upstream rotates them.
    unstable_examples=("MiniMax-Voice-Clone-Speech-2.6-HD",),
    probe=empty_payload_request_probe,
)

_GMI_AUDIO_MUSIC_FAMILY = ModelFamily(
    name="gmi-audio-music",
    pattern=re.compile(r"^MiniMax-Music"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        extras={**_ENVELOPE, "is_music": True},
        **_MUSIC.build(),
    ),
    description="GMICloud music generation (MiniMax Music family).",
    example_slugs=("MiniMax-Music-2.5",),
    unstable_examples=("MiniMax-Music-2.5",),
    probe=empty_payload_request_probe,
)

_GMI_AUDIO_TTS_FAMILY = ModelFamily(
    name="gmi-audio-tts",
    # ElevenLabs / MiniMax-TTS / Inworld TTS variants. Voice-Clone and
    # Music get their own families above (more-specific patterns checked
    # first per the family-resolution contract).
    pattern=re.compile(r"^(?:ElevenLabs-TTS|MiniMax-TTS|Inworld-TTS)"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        extras={**_ENVELOPE, "is_music": False},
        **_AUDIO_BASE.build(),
    ),
    description="GMICloud text-to-speech (ElevenLabs, MiniMax-TTS, Inworld families).",
    example_slugs=(
        "ElevenLabs-TTS-v3",
        "MiniMax-TTS-Speech-2.6-Turbo",
        "Inworld-TTS-1.5-Mini",
    ),
    unstable_examples=(
        "ElevenLabs-TTS-v3",
        "MiniMax-TTS-Speech-2.6-Turbo",
        "Inworld-TTS-1.5-Mini",
    ),
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
