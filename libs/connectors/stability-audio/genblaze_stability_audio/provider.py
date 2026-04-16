"""StabilityAudioProvider — adapter for Stability AI Stable Audio API.

Synchronous API: POST multipart form, returns audio bytes directly.

Docs: https://platform.stability.ai/docs/api-reference
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_stability_audio_error

_API_URL = "https://api.stability.ai/v2beta/audio/stable-audio-2/text-to-audio"

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
}

# Per-second pricing for generated audio (USD)
_PRICE_PER_SEC = 0.01


class StabilityAudioProvider(SyncProvider):
    """Provider adapter for Stability AI Stable Audio generation.

    Model: ``stable-audio-2.5`` — generates music and sound effects up to 3 min.

    Uses raw HTTP (no SDK) since Stability has no official Python SDK for audio.

    Args:
        api_key: Stability AI API key. Falls back to STABILITY_API_KEY env var.
        http_timeout: HTTP request timeout in seconds (default 120).
        output_dir: Directory for output audio files (default system temp).
    """

    name = "stability-audio"

    def get_capabilities(self) -> ProviderCapabilities:
        """Stability Audio: music and sound effect generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            max_duration=190.0,
            models=["stable-audio-2.5"],
            output_formats=["audio/mpeg", "audio/wav", "audio/ogg"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        http_timeout: float = 120.0,
        output_dir: str | Path | None = None,
    ):
        super().__init__()
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._http_client: Any = None

    def _get_http_client(self):
        if self._http_client is None:
            try:
                import httpx
            except ImportError as exc:
                raise ProviderError("httpx package not installed. Run: pip install httpx") from exc
            self._http_client = httpx.Client(timeout=self._http_timeout)
        return self._http_client

    def close(self) -> None:
        """Close the HTTP client and release connection pool resources."""
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        import os

        key = os.environ.get("STABILITY_API_KEY")
        if not key:
            raise ProviderError(
                "No API key. Set STABILITY_API_KEY env var or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return key

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate audio via Stability AI Stable Audio API."""
        client = self._get_http_client()
        api_key = self._get_api_key()
        try:
            output_format = step.params.get("output_format", "mp3")
            media_type = _FORMAT_TO_MIME.get(output_format, "audio/mpeg")

            form_data: dict = {
                "prompt": step.prompt or "",
                "output_format": output_format,
            }

            if "duration" in step.params:
                dur = float(step.params["duration"])
                if dur < 0.5 or dur > 190:
                    raise ProviderError(
                        f"Invalid duration={dur}. Must be 0.5–190 seconds.",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                form_data["duration"] = str(dur)

            if step.seed is not None:
                form_data["seed"] = str(step.seed)

            response = client.post(
                _API_URL,
                data=form_data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "audio/*",
                },
            )

            if response.status_code != 200:
                raise ProviderError(
                    f"Stability Audio API error {response.status_code}: {response.text[:200]}",
                    error_code=map_stability_audio_error(Exception(), response.status_code),
                )

            ext = f".{output_format}"
            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{ext}"
            else:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(response.content)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "music"
            asset.size_bytes = len(response.content)
            # Stable Audio outputs stereo audio
            asset.audio = AudioMetadata(channels=2, codec=output_format)

            # Probe actual audio duration, fall back to requested duration
            from genblaze_core._utils import probe_audio_duration

            actual_dur = probe_audio_duration(out_path)
            if actual_dur is not None:
                asset.duration = actual_dur
            elif "duration" in step.params:
                asset.duration = float(step.params["duration"])

            if asset.duration is not None:
                step.cost_usd = asset.duration * _PRICE_PER_SEC

            step.assets.append(asset)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Stability Audio generation failed: {exc}",
                error_code=map_stability_audio_error(exc),
            ) from exc
