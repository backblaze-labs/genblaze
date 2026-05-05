"""NvidiaAudioProvider — audio generation via NVIDIA NIM (Fugatto, Riva TTS).

Most NVIDIA audio endpoints return inline base64 audio. This provider is
synchronous; if an endpoint returns 202 we short-poll NVCF inside
``generate()`` so the caller still sees a single blocking call.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoverySupport,
    LiveProbeResult,
)
from genblaze_core.providers.base import (
    ProviderCapabilities,
    SyncProvider,
    validate_asset_url,
)
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.retry import RetryPolicy, retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._base import (
    NvidiaClient,
    extract_asset_urls,
    extract_base64_assets,
    extract_error_detail,
    save_bytes_to_output_dir,
)
from ._errors import map_nvidia_error
from .models.audio import build_audio_registry


class NvidiaAudioProvider(SyncProvider):
    """Adapter for NVIDIA NIM audio generation.

    Args:
        api_key: NVIDIA API key. Falls back to NVIDIA_API_KEY env var.
        http_timeout: HTTP request timeout in seconds.
        gen_base_url: Override the generation base URL (self-hosted NIM).
        nvcf_status_url: Override the NVCF async-status URL.
        nvcf_timeout: Max seconds to wait for async (202) completions.
        output_dir: Where to save inline base64 payloads. Defaults to CWD.
        http_client: Pre-built httpx.Client for tests / shared pools.
        models: Optional custom ModelRegistry.
    """

    name = "nvidia-audio"
    discovery_support = DiscoverySupport.PARTIAL
    """NVIDIA's generative endpoints have no ``GET /models`` catalog. The
    ``ModelFamily``-attached empty-payload probe is the authoritative
    liveness signal — see ``_invoke_family_probe`` and ``_probe.py``."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return build_audio_registry()

    def _invoke_family_probe(self, probe: Any, model_id: str) -> LiveProbeResult:
        """Forward the family probe with this provider's ``httpx.Client``."""
        return probe(model_id, http=self._client.http())

    def __init__(
        self,
        api_key: str | None = None,
        *,
        http_timeout: float = 120.0,
        gen_base_url: str | None = None,
        nvcf_status_url: str | None = None,
        nvcf_timeout: float = 120.0,
        output_dir: Path | str | None = None,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(models=models, retry_policy=retry_policy)
        self._output_dir: Path | None = Path(output_dir) if output_dir is not None else None
        self._nvcf_timeout = nvcf_timeout
        self._client = NvidiaClient(
            api_key=api_key,
            gen_base_url=gen_base_url,
            nvcf_status_url=nvcf_status_url,
            http_timeout=http_timeout,
            http_client=http_client,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text", "audio"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/wav", "audio/ogg"],
        )

    def close(self) -> None:
        """Release httpx resources for internally-created clients."""
        self._client.close()

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        try:
            payload = self.prepare_payload(step)
            status, body, headers = self._client.post_generation(step.model, payload)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"NVIDIA audio generate failed: {exc}",
                error_code=map_nvidia_error(exc),
            ) from exc

        if status == 202:
            req_id = headers.get("nvcf-reqid")
            if not req_id:
                raise ProviderError(
                    "NVIDIA returned 202 without an NVCF-REQID header",
                    error_code=ProviderErrorCode.SERVER_ERROR,
                )
            body = self._client.wait_for_nvcf(str(req_id), timeout=self._nvcf_timeout)
        elif status != 200:
            detail = extract_error_detail(body)
            raise ProviderError(
                f"NVIDIA audio generate failed ({status}): {detail}",
                error_code=map_nvidia_error(Exception(detail), status),
                retry_after=retry_after_from_response(headers),
            )

        step.provider_payload = {"nvidia": {"status": "succeeded"}}

        # Music models produce stereo; TTS mono. Spec.extras flags which.
        spec = self._models.get(step.model)
        is_music = bool(spec.extras.get("is_music"))

        urls = extract_asset_urls(body)
        if urls:
            self._attach_url_assets(step, urls, is_music=is_music)
            return step

        payloads = extract_base64_assets(body)
        if payloads:
            for raw, mime in payloads:
                media_type = mime or "audio/mpeg"
                ext = mimetypes.guess_extension(media_type) or ".mp3"
                url = save_bytes_to_output_dir(
                    raw, self._output_dir, extension=ext, prefix="nvidia-audio"
                )
                asset = Asset(url=url, media_type=media_type)
                asset.audio = AudioMetadata(
                    channels=2 if is_music else 1,
                    codec=_codec_from_mime(media_type),
                )
                step.assets.append(asset)
            return step

        raise ProviderError(
            "NVIDIA audio response contained no asset URL or base64 payload",
            error_code=ProviderErrorCode.SERVER_ERROR,
        )

    def _attach_url_assets(self, step: Step, urls: list[str], *, is_music: bool) -> None:
        """Attach hosted URLs as Assets atomically."""
        new_assets: list[Asset] = []
        for url in urls:
            validate_asset_url(url)
            path = urlparse(url).path
            mime, _ = mimetypes.guess_type(path)
            if mime is None or not mime.startswith("audio/"):
                mime = "audio/mpeg"
            asset = Asset(url=url, media_type=mime)
            asset.audio = AudioMetadata(
                channels=2 if is_music else 1,
                codec=_codec_from_mime(mime),
            )
            new_assets.append(asset)
        step.assets.extend(new_assets)


def _codec_from_mime(media_type: str) -> str:
    """Map a MIME type to a stored ``AudioMetadata.codec`` short name."""
    if "mpeg" in media_type or "mp3" in media_type:
        return "mp3"
    if "wav" in media_type or "wave" in media_type:
        return "pcm"
    if "ogg" in media_type:
        return "vorbis"
    if "flac" in media_type:
        return "flac"
    return "mp3"
