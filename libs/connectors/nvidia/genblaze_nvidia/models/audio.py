"""NVIDIA audio-model specs (Fugatto, Riva TTS, Maxine).

NVIDIA's build.nvidia.com audio catalog is smaller and more fluid than the
image/video side; keep the curated list minimal and permissive. Unlisted
models pass through the fallback spec.
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    route_audio,
)

_COMMON_INPUT = route_audio(slot="audio")


def _audio_spec(model_id: str, *, is_music: bool = False) -> ModelSpec:
    """Build a spec. ``is_music`` drives stereo-channel metadata downstream."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.AUDIO,
        pricing=None,
        input_mapping=_COMMON_INPUT,
        extras={"is_music": is_music},
    )


_AUDIO_MODELS: tuple[tuple[str, bool], ...] = (
    # (model_id, is_music)
    ("nvidia/fugatto", True),
    ("nvidia/riva-tts", False),
    ("nvidia/maxine-voice-font", False),
)


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    input_mapping=_COMMON_INPUT,
)


def build_audio_registry() -> ModelRegistry:
    """Return the default audio ModelRegistry."""
    defaults = {mid: _audio_spec(mid, is_music=is_music) for mid, is_music in _AUDIO_MODELS}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
