"""OpenAITTSProvider — adapter for the OpenAI Text-to-Speech API.

Synchronous API: POST /v1/audio/speech returns audio bytes directly.

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Per-1M-character pricing per model tier
is declared on the spec; users can override via ``models=`` kwarg.

Docs: https://platform.openai.com/docs/api-reference/audio/createSpeech
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    EnumSchema,
    FloatSchema,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
    per_input_chars,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_openai._errors import map_openai_error

_VALID_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
    }
)

_VALID_RESPONSE_FORMATS = frozenset({"mp3", "opus", "aac", "flac", "wav", "pcm"})

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}

# Per-1M character pricing by model (USD)
_TTS_PER_1M_RATES: dict[str, float] = {
    "tts-1": 15.00,
    "tts-1-hd": 30.00,
    "gpt-4o-mini-tts": 12.00,
}


def _tts_spec(model_id: str, rate_per_1m: float) -> ModelSpec:
    """Per-model spec — per-1M-character pricing on ``step.prompt``."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.AUDIO,
        pricing=per_input_chars(rate_per_1m, per=1_000_000),
        param_coercers={"speed": float},
        param_schemas={
            "voice": EnumSchema(values=_VALID_VOICES),
            "response_format": EnumSchema(values=_VALID_RESPONSE_FORMATS),
            "speed": FloatSchema(min=0.25, max=4.0),
        },
    )


class OpenAITTSProvider(SyncProvider):
    """Provider adapter for OpenAI Text-to-Speech.

    Models: ``tts-1`` (fast), ``tts-1-hd`` (high quality), ``gpt-4o-mini-tts``.

    The TTS API returns audio bytes directly (synchronous). Since there's
    no CDN URL, output is saved to a temp file and the local file URI is
    used as the asset URL. For production, pair with an ObjectStorageSink
    to upload to S3/B2.

    Args:
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        http_timeout: HTTP request timeout in seconds (default 60).
        output_dir: Directory for temp audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "openai-tts"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        defaults = {mid: _tts_spec(mid, rate) for mid, rate in _TTS_PER_1M_RATES.items()}
        return ModelRegistry(defaults=defaults)

    def get_capabilities(self) -> ProviderCapabilities:
        """OpenAI TTS: audio speech generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/opus", "audio/aac", "audio/flac", "audio/wav"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        http_timeout: float = 60.0,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(models=models, retry_policy=retry_policy)
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError as exc:
                raise ProviderError(
                    "openai package not installed. Run: pip install openai"
                ) from exc
            kwargs: dict = {"timeout": self._http_timeout}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate speech audio via the OpenAI TTS API."""
        client = self._get_client()
        try:
            # Run the spec pipeline — coerces speed to float, validates enums.
            payload = self.prepare_payload(step)

            voice = payload.get("voice", "alloy")
            response_format = payload.get("response_format", "mp3")
            media_type = _FORMAT_TO_MIME.get(response_format, "audio/mpeg")

            params: dict = {
                "model": step.model,
                "input": payload.get("prompt", step.prompt or ""),
                "voice": voice,
                "response_format": response_format,
            }
            if "speed" in payload:
                params["speed"] = payload["speed"]
            if "instructions" in payload:
                params["instructions"] = payload["instructions"]

            response = client.audio.speech.create(**params)

            suffix = f".{response_format}"
            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{suffix}"
                response.write_to_file(str(out_path))
            else:
                fd, tmp = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                out_path = Path(tmp)
                response.write_to_file(str(out_path))

            # Use file URI — upload to cloud storage via ObjectStorageSink
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "speech"
            asset.audio = AudioMetadata(channels=1, codec=response_format)
            asset.size_bytes = out_path.stat().st_size
            # Probe actual audio duration (requires mutagen — optional dep)
            from genblaze_core._utils import probe_audio_duration

            dur = probe_audio_duration(out_path)
            if dur is not None:
                asset.duration = dur
            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"TTS generation failed: {exc}",
                error_code=map_openai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
