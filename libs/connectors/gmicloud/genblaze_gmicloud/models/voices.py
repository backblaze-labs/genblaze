"""Curated voice catalog for GMICloud TTS / voice-clone models.

Static catalog refreshed manually from each upstream's published voice list
(quarterly cadence — see ``provider-standardization-tranche.md``). A live
``list_voices()`` would require N upstream calls per request; for GMI the
catalogs change rarely and a static list is faster + offline-friendly.

Each :class:`Voice` declares the ``model`` it belongs to so the provider's
``list_voices(model=...)`` filter narrows correctly. Voices with
``model=None`` work across every audio model the provider exposes.

When upstream confirms a new voice, add it here and bump
``_VOICES_LAST_VERIFIED``.
"""

from __future__ import annotations

from genblaze_core.models.voice import Voice

_PROVIDER = "gmicloud-audio"
_VOICES_LAST_VERIFIED = "2026-04-24"


# ElevenLabs-TTS-v3 — selection from the curated ElevenLabs default-voice
# library (id values are the ElevenLabs ``voice_id`` strings; valid against
# the GMICloud TTS proxy as of the last refresh).
_ELEVENLABS_TTS_VOICES: tuple[Voice, ...] = (
    Voice(
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        name="George",
        provider=_PROVIDER,
        model="ElevenLabs-TTS-v3",
        gender="male",
        language="en-US",
        style_tags=("warm", "narration"),
    ),
    Voice(
        voice_id="EXAVITQu4vr4xnSDxMaL",
        name="Sarah",
        provider=_PROVIDER,
        model="ElevenLabs-TTS-v3",
        gender="female",
        language="en-US",
        style_tags=("conversational", "young"),
    ),
    Voice(
        voice_id="ErXwobaYiN019PkySvjV",
        name="Antoni",
        provider=_PROVIDER,
        model="ElevenLabs-TTS-v3",
        gender="male",
        language="en-US",
        style_tags=("calm", "narration"),
    ),
    Voice(
        voice_id="21m00Tcm4TlvDq8ikWAM",
        name="Rachel",
        provider=_PROVIDER,
        model="ElevenLabs-TTS-v3",
        gender="female",
        language="en-US",
        style_tags=("clear", "narration"),
    ),
    Voice(
        voice_id="pNInz6obpgDQGcFmaJgB",
        name="Adam",
        provider=_PROVIDER,
        model="ElevenLabs-TTS-v3",
        gender="male",
        language="en-US",
        style_tags=("deep", "announcer"),
    ),
)


# MiniMax-TTS-Speech-2.6-Turbo — MiniMax's published default voice ids.
_MINIMAX_TTS_VOICES: tuple[Voice, ...] = (
    Voice(
        voice_id="male-qn-qingse",
        name="Qingse (Male)",
        provider=_PROVIDER,
        model="MiniMax-TTS-Speech-2.6-Turbo",
        gender="male",
        language="zh-CN",
        style_tags=("youth", "clear"),
    ),
    Voice(
        voice_id="female-shaonv",
        name="Shaonv (Female)",
        provider=_PROVIDER,
        model="MiniMax-TTS-Speech-2.6-Turbo",
        gender="female",
        language="zh-CN",
        style_tags=("youth", "warm"),
    ),
    Voice(
        voice_id="male-qn-jingying",
        name="Jingying (Male)",
        provider=_PROVIDER,
        model="MiniMax-TTS-Speech-2.6-Turbo",
        gender="male",
        language="zh-CN",
        style_tags=("professional",),
    ),
    Voice(
        voice_id="presenter_female",
        name="Presenter (Female)",
        provider=_PROVIDER,
        model="MiniMax-TTS-Speech-2.6-Turbo",
        gender="female",
        language="en-US",
        style_tags=("presenter", "announcer"),
    ),
)


# Inworld-TTS-1.5-Mini — Inworld AI character voices, English-only.
_INWORLD_TTS_VOICES: tuple[Voice, ...] = (
    Voice(
        voice_id="ashley",
        name="Ashley",
        provider=_PROVIDER,
        model="Inworld-TTS-1.5-Mini",
        gender="female",
        language="en-US",
        style_tags=("conversational", "warm"),
    ),
    Voice(
        voice_id="ronald",
        name="Ronald",
        provider=_PROVIDER,
        model="Inworld-TTS-1.5-Mini",
        gender="male",
        language="en-US",
        style_tags=("conversational",),
    ),
)


_ALL_VOICES: tuple[Voice, ...] = (
    *_ELEVENLABS_TTS_VOICES,
    *_MINIMAX_TTS_VOICES,
    *_INWORLD_TTS_VOICES,
)


def list_curated_voices(*, model: str | None = None, language: str | None = None) -> list[Voice]:
    """Return curated voices, optionally filtered by model and BCP 47 prefix.

    Filter semantics:
    - ``model``: exact match against ``Voice.model``. Voices with
      ``Voice.model is None`` (cross-model voices) always pass.
    - ``language``: BCP 47 prefix match (``"en"`` → ``"en-US"``, ``"en-GB"``).
      Voices without a language declared are excluded when this filter is set.
    """
    out: list[Voice] = []
    for voice in _ALL_VOICES:
        if model is not None and voice.model is not None and voice.model != model:
            continue
        if language is not None:
            if voice.language is None:
                continue
            # BCP 47 primary-subtag equality: ``"en"`` matches both ``"en-US"``
            # and ``"en-GB"`` but not ``"es"``. Use ``==`` not ``startswith``
            # — a single-character user input ``"e"`` should NOT match every
            # English voice.
            voice_primary = voice.language.split("-", 1)[0].lower()
            filter_primary = language.split("-", 1)[0].lower()
            if voice_primary != filter_primary:
                continue
        out.append(voice)
    return out
