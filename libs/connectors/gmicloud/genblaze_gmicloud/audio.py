"""GMICloudAudioProvider — audio/TTS generation via the GMICloud request queue.

Auth: Set GMI_API_KEY env var or pass api_key= to the constructor.

Docs: https://docs.gmicloud.ai
"""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    ProviderCapabilities,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from ._base import GMICloudBase
from ._errors import map_gmicloud_error

# Per-generation pricing by model (USD) — approximate, based on GMICloud tiers.
# Unknown models pass through to the API; cost_usd will be None.
_AUDIO_PRICING: dict[str, float] = {
    "ElevenLabs-TTS-v3": 0.10,
    "MiniMax-TTS-Speech-2.6-Turbo": 0.06,
    "MiniMax-Voice-Clone-Speech-2.6-HD": 0.10,
    "Inworld-TTS-1.5-Mini": 0.005,
    "MiniMax-Music-2.5": 0.15,
}

# Music models produce stereo output; TTS models produce mono
_MUSIC_MODELS: set[str] = {"MiniMax-Music-2.5"}

# Voice cloning models accept a reference audio sample via step.inputs[0].
# Payload field name follows MiniMax's native voice-clone convention; confirm
# against the GMICloud request-queue schema on first live integration.
_VOICE_CLONE_MODELS: set[str] = {"MiniMax-Voice-Clone-Speech-2.6-HD"}


class GMICloudAudioProvider(GMICloudBase):
    """Provider adapter for GMICloud audio/TTS generation via the request queue.

    Models: ElevenLabs TTS, MiniMax TTS/Music, Inworld TTS, and any new audio
    model added to GMICloud's queue (unknown models pass through).

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
    """

    name = "gmicloud-audio"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text", "audio"],
            accepts_chain_input=True,
            models=sorted(_AUDIO_PRICING),
            output_formats=["audio/mpeg", "audio/wav"],
        )

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        p = dict(params)
        if "voice" in p and "voice_id" not in p:
            p["voice_id"] = p.pop("voice")
        return p

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        try:
            payload: dict = {}
            if step.prompt:
                payload["prompt"] = step.prompt

            for key in ("voice_id", "language", "duration", "output_format"):
                if key in step.params:
                    payload[key] = step.params[key]

            if step.seed is not None:
                payload["seed"] = step.seed

            # Always SSRF-validate any chain input; forward to payload only for
            # voice-clone models that actually consume a reference audio sample.
            if step.inputs:
                validate_chain_input_url(step.inputs[0].url)
                if step.model in _VOICE_CLONE_MODELS:
                    payload["reference_audio"] = step.inputs[0].url

            return self._submit_request(step.model, payload)

        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud submit failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        try:
            detail = self._fetch_detail(prediction_id)
            status = detail.get("status", "")
            outcome = detail.get("outcome") or {}

            step.provider_payload = {"gmicloud": {"request_id": prediction_id, "status": status}}

            if status in ("failed", "cancelled"):
                raise ProviderError(
                    str(detail.get("error") or f"Audio generation {status}"),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            audio_url = outcome.get("audio_url") or outcome.get("url")
            if not audio_url:
                raise ProviderError("GMICloud request completed but no audio URL found")

            validate_asset_url(str(audio_url))

            path = urlparse(str(audio_url)).path
            mime, _ = mimetypes.guess_type(path)
            if mime is None or not mime.startswith("audio/"):
                mime = "audio/mpeg"

            asset = Asset(url=str(audio_url), media_type=mime)
            asset.audio = AudioMetadata(
                channels=2 if step.model in _MUSIC_MODELS else 1,
                codec="mp3",
            )
            step.assets.append(asset)

            per_gen = _AUDIO_PRICING.get(step.model)
            if per_gen is not None:
                step.cost_usd = per_gen * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud fetch_output failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc
