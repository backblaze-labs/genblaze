"""ElevenLabsSFXProvider — adapter for ElevenLabs Sound Effects API.

Synchronous API: returns audio bytes directly.

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Users can override pricing or register
new models via::

    provider = ElevenLabsSFXProvider(models=my_registry)

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
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    SyncProvider,
    bucketed_by_duration,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_elevenlabs._errors import map_elevenlabs_error

# Duration-bucketed pricing (USD). (min_inclusive, max_exclusive) → price.
# Legacy buckets: short ≤5s, medium >5s and ≤15s, long >15s up to 30s.
# Translated to the ``[lo, hi)`` shape the packaged strategy expects while
# preserving the inclusive upper bounds of the legacy buckets.
_SFX_DURATION_BUCKETS: list[tuple[tuple[float, float], float]] = [
    ((0.0, 5.0 + 1e-9), 0.10),  # ≤5s
    ((5.0 + 1e-9, 15.0 + 1e-9), 0.20),  # >5s and ≤15s
    ((15.0 + 1e-9, 30.0 + 1e-9), 0.30),  # >15s up to 30s
]


def _sfx_spec(model_id: str) -> ModelSpec:
    """Single-model spec — duration-bucketed pricing."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.AUDIO,
        pricing=bucketed_by_duration(_SFX_DURATION_BUCKETS),
    )


class ElevenLabsSFXProvider(SyncProvider):
    """Provider adapter for ElevenLabs Sound Effects generation.

    Model: ``eleven_text_to_sound_v2``.

    Generates sound effects from text descriptions (e.g., "thunder crashing",
    "footsteps on gravel"). Duration 0.5–30 seconds.

    Args:
        api_key: ElevenLabs API key. Falls back to ELEVENLABS_API_KEY env var.
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "elevenlabs-sfx"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            defaults={"eleven_text_to_sound_v2": _sfx_spec("eleven_text_to_sound_v2")}
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """ElevenLabs SFX: sound effect generation from text descriptions."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            max_duration=30.0,
            models=self._models.known(),
            output_formats=["audio/mpeg"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(models=models, retry_policy=retry_policy)
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
            # Preserve bespoke "Invalid duration_seconds" wording tests assert on.
            if "duration_seconds" in step.params:
                try:
                    dur = float(step.params["duration_seconds"])
                except (TypeError, ValueError) as exc:
                    raise ProviderError(
                        f"Invalid duration_seconds={step.params['duration_seconds']!r}. "
                        "Must be 0.5–30.",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    ) from exc
                if dur < 0.5 or dur > 30:
                    raise ProviderError(
                        f"Invalid duration_seconds={dur}. Must be 0.5–30.",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )

            payload = self.prepare_payload(step)

            kwargs: dict = {
                "text": payload.get("prompt", step.prompt or ""),
            }

            if "duration_seconds" in payload:
                kwargs["duration_seconds"] = float(payload["duration_seconds"])
            if "prompt_influence" in payload:
                kwargs["prompt_influence"] = float(payload["prompt_influence"])

            # text_to_sound_effects.convert() returns audio bytes iterator
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

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"ElevenLabs SFX failed: {exc}",
                error_code=map_elevenlabs_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
