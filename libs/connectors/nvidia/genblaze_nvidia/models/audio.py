"""NVIDIA audio-model families (Fugatto music + Magpie/Riva/Maxine voice).

Two families:

* ``nvidia-audio-music`` — Fugatto. Stereo output (extras["is_music"]=True).
* ``nvidia-audio-voice`` — TTS / voice-clone / Maxine. Mono output.

Both families ship the empty-payload genai probe so
``Pipeline.preflight()`` can surface dead slugs before the wire — the
fix for F-2026-05-04-01 (``nvidia/riva-tts`` 404 mid-pipeline). The
historical ``nvidia/riva-tts`` slug is **not** included as an
``example_slug`` because NIM has retired it; users still passing that id
will hit ``NOT_FOUND`` at preflight via the probe, with
``nvidia/magpie-tts-multilingual`` suggested as the replacement.
"""

from __future__ import annotations

import re

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    route_audio,
)

from .._probe import empty_payload_genai_probe

_AUDIO_INPUT = route_audio(slot="audio")


_NVIDIA_AUDIO_MUSIC_FAMILY = ModelFamily(
    name="nvidia-audio-music",
    pattern=re.compile(r"^nvidia/fugatto"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        input_mapping=_AUDIO_INPUT,
        extras={"is_music": True},
    ),
    description="NVIDIA Fugatto family — text/audio-conditioned music generation.",
    example_slugs=("nvidia/fugatto",),
    probe=empty_payload_genai_probe,
)

_NVIDIA_AUDIO_VOICE_FAMILY = ModelFamily(
    name="nvidia-audio-voice",
    # Magpie-TTS is the active replacement for the retired Riva-TTS slug;
    # both name fragments stay in the pattern in case NIM ships further
    # variants (riva-tts-multilingual etc.) without changing param shape.
    pattern=re.compile(r"^nvidia/(?:magpie-tts|riva-tts|maxine-)"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        input_mapping=_AUDIO_INPUT,
        extras={"is_music": False},
    ),
    description="NVIDIA voice family — Magpie-TTS, Riva-TTS variants, Maxine voice.",
    example_slugs=(
        "nvidia/magpie-tts-multilingual",
        "nvidia/maxine-voice-font",
    ),
    probe=empty_payload_genai_probe,
)


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    input_mapping=_AUDIO_INPUT,
)


def build_audio_registry() -> ModelRegistry:
    """Return the default audio ``ModelRegistry`` — pattern-keyed, no slugs."""
    return ModelRegistry(
        provider_families=(_NVIDIA_AUDIO_MUSIC_FAMILY, _NVIDIA_AUDIO_VOICE_FAMILY),
        fallback=_FALLBACK,
    )
