"""GMICloud audio-model specs (TTS, voice-clone, music)."""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    per_unit,
    route_audio,
)

_AUDIO_FLAT: dict[str, float] = {
    "ElevenLabs-TTS-v3": 0.10,
    "MiniMax-TTS-Speech-2.6-Turbo": 0.06,
    "MiniMax-Voice-Clone-Speech-2.6-HD": 0.10,
    "Inworld-TTS-1.5-Mini": 0.005,
    "MiniMax-Music-2.5": 0.15,
}

# Models that consume a reference audio sample via chain inputs.
_VOICE_CLONE_MODELS = frozenset({"MiniMax-Voice-Clone-Speech-2.6-HD"})

_COMMON_ALIASES = {"voice": "voice_id"}
_COMMON_ALLOWLIST = frozenset(
    {"prompt", "seed", "voice_id", "language", "duration", "output_format", "reference_audio"}
)
_CLONE_INPUT = route_audio(slot="reference_audio")
_ENVELOPE = {"envelope_key": "payload"}


def _audio_spec(model_id: str, flat_rate: float) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        modality=Modality.AUDIO,
        pricing=per_unit(flat_rate),
        param_aliases=_COMMON_ALIASES,
        param_allowlist=_COMMON_ALLOWLIST,
        input_mapping=_CLONE_INPUT if model_id in _VOICE_CLONE_MODELS else None,
        extras={**_ENVELOPE, "is_music": model_id == "MiniMax-Music-2.5"},
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    param_aliases=_COMMON_ALIASES,
    extras=_ENVELOPE,
)


def build_audio_registry() -> ModelRegistry:
    defaults = {mid: _audio_spec(mid, rate) for mid, rate in _AUDIO_FLAT.items()}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
