"""ElevenLabsTTSProvider — adapter for ElevenLabs Text-to-Speech API.

Synchronous API: returns audio bytes directly.

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Users can override pricing or register
new models via::

    provider = ElevenLabsTTSProvider(models=my_registry)

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, WordTiming
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    SyncProvider,
    per_input_chars,
)
from genblaze_core.runnable.config import RunnableConfig

from genblaze_elevenlabs._errors import map_elevenlabs_error

_FORMAT_TO_MIME = {
    "mp3_44100_128": "audio/mpeg",
    "mp3_44100_192": "audio/mpeg",
    "mp3_44100_64": "audio/mpeg",
    "mp3_44100_32": "audio/mpeg",
    "mp3_22050_32": "audio/mpeg",
    "pcm_16000": "audio/pcm",
    "pcm_22050": "audio/pcm",
    "pcm_24000": "audio/pcm",
    "pcm_44100": "audio/pcm",
    "wav_44100": "audio/wav",
    "opus_48000_128": "audio/opus",
}

_FORMAT_TO_EXT = {
    "audio/mpeg": ".mp3",
    "audio/pcm": ".pcm",
    "audio/wav": ".wav",
    "audio/opus": ".opus",
}

# Per-1K character pricing by model tier (USD). Baked into ``per_input_chars``
# strategies on the model specs.
_ELEVENLABS_PER_1K_RATES: dict[str, float] = {
    "eleven_v3": 0.30,
    "eleven_multilingual_v2": 0.30,
    "eleven_flash_v2_5": 0.08,
    "eleven_turbo_v2_5": 0.15,
}


def _tts_spec(model_id: str, rate_per_1k: float) -> ModelSpec:
    """Per-model spec — per-1K-character pricing on ``step.prompt``."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.AUDIO,
        pricing=per_input_chars(rate_per_1k, per=1000),
    )


def _parse_elevenlabs_alignment(
    chars: list[str],
    starts: list[float],
    ends: list[float],
) -> list[WordTiming]:
    """Build WordTiming list from ElevenLabs character-level alignment.

    Groups consecutive characters into words (split on spaces) and uses the
    first character's start and last character's end as word boundaries.
    """
    timings: list[WordTiming] = []
    current_word = ""
    word_start: float | None = None
    word_end: float = 0.0

    for ch, s, e in zip(chars, starts, ends, strict=False):
        if ch == " ":
            if current_word:
                wt = WordTiming(word=current_word, start=word_start or 0.0, end=word_end)
                timings.append(wt)
                current_word = ""
                word_start = None
        else:
            if word_start is None:
                word_start = s
            current_word += ch
            word_end = e

    # Flush last word
    if current_word:
        timings.append(WordTiming(word=current_word, start=word_start or 0.0, end=word_end))

    return timings


class ElevenLabsTTSProvider(SyncProvider):
    """Provider adapter for ElevenLabs Text-to-Speech.

    Models: ``eleven_v3``, ``eleven_multilingual_v2``, ``eleven_flash_v2_5``,
    ``eleven_turbo_v2_5``.

    Args:
        api_key: ElevenLabs API key. Falls back to ELEVENLABS_API_KEY env var.
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "elevenlabs-tts"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        defaults = {mid: _tts_spec(mid, rate) for mid, rate in _ELEVENLABS_PER_1K_RATES.items()}
        return ModelRegistry(defaults=defaults)

    def get_capabilities(self) -> ProviderCapabilities:
        """ElevenLabs TTS: audio speech generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/pcm", "audio/wav", "audio/opus"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
    ):
        super().__init__(models=models)
        self._api_key = api_key
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                from elevenlabs.client import ElevenLabs
            except ImportError as exc:
                raise ProviderError(
                    "elevenlabs package not installed. Run: pip install elevenlabs"
                ) from exc
            kwargs: dict = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = ElevenLabs(**kwargs)
        return self._client

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate speech audio via ElevenLabs TTS API."""
        client = self._get_client()
        try:
            # Run the spec pipeline; result mostly mirrors step.params since
            # the spec is permissive apart from pricing.
            payload = self.prepare_payload(step)

            voice_id = payload.get("voice_id", "JBFqnCBsd6RMkjVDRZzb")
            output_format = payload.get("output_format", "mp3_44100_128")
            media_type = _FORMAT_TO_MIME.get(output_format, "audio/mpeg")
            ext = _FORMAT_TO_EXT.get(media_type, ".mp3")

            kwargs: dict = {
                "text": payload.get("prompt", step.prompt or ""),
                "voice_id": voice_id,
                "model_id": step.model,
                "output_format": output_format,
            }

            voice_settings: dict = {}
            if "stability" in payload:
                voice_settings["stability"] = float(payload["stability"])
            if "similarity_boost" in payload:
                voice_settings["similarity_boost"] = float(payload["similarity_boost"])
            if "style" in payload:
                voice_settings["style"] = float(payload["style"])
            if voice_settings:
                kwargs["voice_settings"] = voice_settings

            if "language_code" in payload:
                kwargs["language_code"] = payload["language_code"]
            if step.seed is not None:
                kwargs["seed"] = step.seed

            # Use timestamps endpoint when requested for word-level timing data.
            # This dispatch stays in generate() — distinct response shape.
            word_timings: list[WordTiming] | None = None
            if payload.get("with_timestamps"):
                response = client.text_to_speech.convert_with_timestamps(**kwargs)
                import base64

                audio_bytes = base64.b64decode(response.get("audio_base64", ""))
                alignment = response.get("alignment", {})
                al_chars = alignment.get("characters", [])
                starts = alignment.get("character_start_times_seconds", [])
                ends = alignment.get("character_end_times_seconds", [])
                if al_chars and starts and ends:
                    word_timings = _parse_elevenlabs_alignment(al_chars, starts, ends)
            else:
                # convert() returns an iterator of audio bytes
                audio_iter = client.text_to_speech.convert(**kwargs)
                audio_bytes = b"".join(audio_iter)

            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{ext}"
            else:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(audio_bytes)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "speech"
            asset.size_bytes = len(audio_bytes)
            # Probe actual audio duration (requires mutagen — optional dep)
            from genblaze_core._utils import probe_audio_duration

            dur = probe_audio_duration(out_path)
            if dur is not None:
                asset.duration = dur

            # Populate audio metadata from output_format (e.g. "mp3_44100_128")
            audio_meta: dict[str, Any] = {"channels": 1}
            parts = output_format.split("_")
            if parts:
                audio_meta["codec"] = parts[0]
            if len(parts) >= 2 and parts[1].isdigit():
                audio_meta["sample_rate"] = int(parts[1])
            if len(parts) >= 3 and parts[2].isdigit():
                audio_meta["bitrate"] = int(parts[2]) * 1000  # kbps → bps
            if word_timings:
                audio_meta["word_timings"] = word_timings
            asset.audio = AudioMetadata(**audio_meta)

            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"ElevenLabs TTS failed: {exc}",
                error_code=map_elevenlabs_error(exc),
            ) from exc
