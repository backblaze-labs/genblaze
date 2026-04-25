"""GMICloud audio-model specs (TTS, voice-clone, music).

Per-model surfaces are composed via :class:`ParamSurface` so each model
declares the exact set of params its upstream accepts. Voice-clone variants
get ``language`` / ``pitch`` / ``emotion``; music gets ``style_weight`` /
``duration_seconds``.

**Reconciliation (2026-04-24):** The 10-agent build report observed every
audio default returning 404 against the live ``/requests`` endpoint. Rather
than removing the specs (which would silently break every caller pinned to
a slug), each suspected-dead model carries ``extras["suspected_dead"] =
True`` so the upcoming probe-CI (``tools/probe_models.py``) surfaces drift
to maintainers without breaking user code mid-migration. Once the probe
confirms, dead specs migrate to ``deprecated_aliases`` of a live successor
or are removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genblaze_core.models.enums import Modality
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ParamSurface,
    per_unit,
    route_audio,
)

# Default audio surface — universally meaningful audio params.
_AUDIO_BASE = ParamSurface.for_modality(Modality.AUDIO).with_aliases(voice="voice_id")

# Voice clone variants accept a reference audio plus expressive controls.
_VOICE_CLONE = _AUDIO_BASE.extend("pitch", "emotion", "speed", "stability", "similarity")

# Music models accept style hints + per-second duration controls.
_MUSIC = _AUDIO_BASE.extend("style_weight", "duration_seconds", "tempo")


@dataclass(frozen=True)
class _AudioModel:
    """Editorial row — one per model in the GMI audio catalog."""

    slug: str
    flat_rate: float
    is_music: bool = False
    is_clone: bool = False
    suspected_dead: bool = False
    deprecated: frozenset[str] = field(default_factory=frozenset)
    surface: ParamSurface = _AUDIO_BASE


# All current audio defaults are flagged ``suspected_dead`` until the probe-CI
# (Phase 4 of the standardization tranche) round-trips a successful response
# against the live API. Removing them prematurely breaks pinned test fixtures
# and user code; flagging them lets the probe gate enforce the contract while
# we coordinate with upstream.
_AUDIO_MODELS: tuple[_AudioModel, ...] = (
    _AudioModel("ElevenLabs-TTS-v3", 0.10, suspected_dead=True),
    _AudioModel("MiniMax-TTS-Speech-2.6-Turbo", 0.06, suspected_dead=True),
    _AudioModel(
        "MiniMax-Voice-Clone-Speech-2.6-HD",
        0.10,
        is_clone=True,
        suspected_dead=True,
        surface=_VOICE_CLONE,
    ),
    _AudioModel("Inworld-TTS-1.5-Mini", 0.005, suspected_dead=True),
    _AudioModel(
        "MiniMax-Music-2.5",
        0.15,
        is_music=True,
        suspected_dead=True,
        surface=_MUSIC,
    ),
)

_ENVELOPE = {"envelope_key": "payload"}
_CLONE_INPUT = route_audio(slot="reference_audio")


def _audio_spec(model: _AudioModel) -> ModelSpec:
    extras: dict[str, object] = {**_ENVELOPE, "is_music": model.is_music}
    if model.suspected_dead:
        extras["suspected_dead"] = True
    return ModelSpec(
        model_id=model.slug,
        modality=Modality.AUDIO,
        deprecated_aliases=model.deprecated,
        pricing=per_unit(model.flat_rate),
        input_mapping=_CLONE_INPUT if model.is_clone else None,
        extras=extras,
        **model.surface.build(),
    )


_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    param_aliases={"voice": "voice_id"},
    extras=_ENVELOPE,
)


def build_audio_registry() -> ModelRegistry:
    defaults = {m.slug: _audio_spec(m) for m in _AUDIO_MODELS}
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
