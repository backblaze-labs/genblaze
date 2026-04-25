"""GMICloudAudioProvider — audio/TTS generation via the GMICloud request queue.

Auth: Set GMI_API_KEY env var or pass api_key= to the constructor.

Docs: https://docs.gmicloud.ai

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
built in ``genblaze_gmicloud.models.audio``.
"""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.models.voice import Voice
from genblaze_core.providers.base import (
    ProviderCapabilities,
    validate_asset_url,
)
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.runnable.config import RunnableConfig

from ._base import GMICloudBase, extract_media_url
from ._errors import map_gmicloud_error
from .models.audio import build_audio_registry
from .models.voices import list_curated_voices


class GMICloudAudioProvider(GMICloudBase):
    """Provider adapter for GMICloud audio / TTS generation via the request queue.

    Models: ElevenLabs TTS, MiniMax TTS / Music, Inworld TTS, and any new audio
    model added to GMICloud's queue (unknown models pass through).

    **This is a generation-only provider.** ``supported_inputs=["text", "audio"]``
    reports that the API accepts text and (optionally) audio. The audio input
    is a *reference voice for cloning* — not a source for speech-to-text. STT
    (audio → text) is out of scope for this class; a separate STT provider may
    ship later.

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
        base_url: Override the request-queue base URL. See ``GMICloudBase``.
        http_client: Pre-built ``httpx.Client``. See ``GMICloudBase``.
        models: Optional custom ``ModelRegistry``.
    """

    name = "gmicloud-audio"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return build_audio_registry()

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text", "audio"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/wav"],
        )

    def list_voices(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> list[Voice]:
        """Curated voice catalog for GMI's TTS / voice-clone models.

        Reads from ``models/voices.py``; refreshed manually each quarter
        rather than fetched live (catalogs change rarely and a static list is
        offline-friendly). To list voices for a specific model::

            provider.list_voices(model="ElevenLabs-TTS-v3")
            provider.list_voices(language="en")  # any English-prefix voice
        """
        return list_curated_voices(model=model, language=language)

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        try:
            payload = self.prepare_payload(step)
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

            audio_url = extract_media_url(outcome)
            if not audio_url:
                raise ProviderError("GMICloud request completed but no audio URL found")

            validate_asset_url(str(audio_url))

            path = urlparse(str(audio_url)).path
            mime, _ = mimetypes.guess_type(path)
            if mime is None or not mime.startswith("audio/"):
                mime = "audio/mpeg"

            asset = Asset(url=str(audio_url), media_type=mime)
            # Music models produce stereo; TTS mono. Spec's extras flags which.
            is_music = bool(self._models.get(step.model).extras.get("is_music"))
            asset.audio = AudioMetadata(
                channels=2 if is_music else 1,
                codec="mp3",
            )
            step.assets.append(asset)
            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud fetch_output failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc
