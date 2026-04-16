"""OpenAITTSProvider — adapter for the OpenAI Text-to-Speech API.

Synchronous API: POST /v1/audio/speech returns audio bytes directly.

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
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

from genblaze_openai._errors import map_openai_error

_VALID_VOICES = {
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

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}

# Per-1M character pricing by model (USD)
_TTS_PRICING: dict[str, float] = {
    "tts-1": 15.00,
    "tts-1-hd": 30.00,
    "gpt-4o-mini-tts": 12.00,
}


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
    """

    name = "openai-tts"

    def get_capabilities(self) -> ProviderCapabilities:
        """OpenAI TTS: audio speech generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            models=["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
            output_formats=["audio/mpeg", "audio/opus", "audio/aac", "audio/flac", "audio/wav"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        http_timeout: float = 60.0,
        output_dir: str | Path | None = None,
    ):
        super().__init__()
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
            voice = step.params.get("voice", "alloy")
            response_format = step.params.get("response_format", "mp3")
            media_type = _FORMAT_TO_MIME.get(response_format, "audio/mpeg")

            params: dict = {
                "model": step.model,
                "input": step.prompt or "",
                "voice": voice,
                "response_format": response_format,
            }
            if "speed" in step.params:
                params["speed"] = float(step.params["speed"])
            if "instructions" in step.params:
                params["instructions"] = step.params["instructions"]

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

            price_per_1m = _TTS_PRICING.get(step.model)
            if price_per_1m is not None:
                chars = len(step.prompt or "")
                step.cost_usd = (chars / 1_000_000) * price_per_1m

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"TTS generation failed: {exc}",
                error_code=map_openai_error(exc),
            ) from exc
