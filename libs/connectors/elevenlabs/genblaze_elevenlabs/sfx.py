"""ElevenLabsSFXProvider — adapter for ElevenLabs Sound Effects API.

Synchronous API: returns audio bytes directly.

Docs: https://elevenlabs.io/docs/api-reference/text-to-sound-effects
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

from genblaze_elevenlabs._errors import map_elevenlabs_error

# Per-generation pricing by duration bucket (USD)
_SFX_PRICING: dict[str, float] = {
    "short": 0.10,  # ≤5s
    "medium": 0.20,  # >5s and ≤15s
    "long": 0.30,  # >15s (max 30s)
}


def _sfx_price_bucket(duration_seconds: float) -> str:
    """Map duration to pricing bucket."""
    if duration_seconds <= 5:
        return "short"
    if duration_seconds <= 15:
        return "medium"
    return "long"


class ElevenLabsSFXProvider(SyncProvider):
    """Provider adapter for ElevenLabs Sound Effects generation.

    Model: ``eleven_text_to_sound_v2``.

    Generates sound effects from text descriptions (e.g., "thunder crashing",
    "footsteps on gravel"). Duration 0.5–30 seconds.

    Args:
        api_key: ElevenLabs API key. Falls back to ELEVENLABS_API_KEY env var.
        output_dir: Directory for output audio files (default system temp).
    """

    name = "elevenlabs-sfx"

    def get_capabilities(self) -> ProviderCapabilities:
        """ElevenLabs SFX: sound effect generation from text descriptions."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            max_duration=30.0,
            models=["eleven_text_to_sound_v2"],
            output_formats=["audio/mpeg"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        output_dir: str | Path | None = None,
    ):
        super().__init__()
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
        """Generate sound effect via ElevenLabs Sound Generation API."""
        client = self._get_client()
        try:
            kwargs: dict = {
                "text": step.prompt or "",
            }

            if "duration_seconds" in step.params:
                dur = float(step.params["duration_seconds"])
                if dur < 0.5 or dur > 30:
                    raise ProviderError(
                        f"Invalid duration_seconds={dur}. Must be 0.5–30.",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                kwargs["duration_seconds"] = dur

            if "prompt_influence" in step.params:
                kwargs["prompt_influence"] = float(step.params["prompt_influence"])

            # sound_generation.convert() returns audio bytes iterator
            audio_iter = client.text_to_sound_effects.convert(**kwargs)
            audio_bytes = b"".join(audio_iter)

            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}.mp3"
            else:
                fd, tmp = tempfile.mkstemp(suffix=".mp3")
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(audio_bytes)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type="audio/mpeg")
            asset.metadata["audio_type"] = "sfx"
            # SFX API returns mp3; mono output
            asset.audio = AudioMetadata(channels=1, codec="mp3")

            # Probe actual output duration, fall back to params
            from genblaze_core._utils import probe_audio_duration

            probed_dur = probe_audio_duration(out_path)
            if probed_dur is None:
                probed_dur = float(step.params.get("duration_seconds", 5))
            asset.duration = probed_dur
            step.assets.append(asset)

            bucket = _sfx_price_bucket(probed_dur)
            step.cost_usd = _SFX_PRICING[bucket]

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"ElevenLabs SFX failed: {exc}",
                error_code=map_elevenlabs_error(exc),
            ) from exc
