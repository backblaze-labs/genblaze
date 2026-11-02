"""LiteLLMSpeechProvider — text-to-speech through the LiteLLM SDK.

One adapter for the speech models LiteLLM routes to: OpenAI ``tts-1`` /
``gpt-4o-mini-tts``, Azure, Vertex AI and Gemini TTS, ElevenLabs, and more.
Models use LiteLLM ids such as ``openai/gpt-4o-mini-tts`` or
``vertex_ai/gemini-2.5-flash-preview-tts``.

Synchronous API: ``litellm.speech`` returns audio bytes directly.

Docs: https://docs.litellm.ai/docs/text_to_speech
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from genblaze_core._utils import local_file_url, probe_audio_duration
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoverySupport,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_litellm._common import credential_kwargs, import_litellm, response_cost
from genblaze_litellm._errors import map_litellm_error

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}

_FALLBACK = ModelSpec(model_id="*", modality=Modality.AUDIO, param_coercers={"speed": float})


class LiteLLMSpeechProvider(SyncProvider):
    """Provider adapter for text-to-speech via LiteLLM.

    ``voice`` defaults to ``alloy``; ``response_format`` (default ``mp3``),
    ``speed``, ``instructions`` and provider-specific keys are forwarded to
    ``litellm.speech``. The audio is written to a local file; pair with an
    ``ObjectStorageSink`` to upload it.

    Args:
        api_key: Provider API key. When omitted, LiteLLM reads the provider's
            own env vars.
        api_base: Custom endpoint (Azure deployment, self-hosted server).
        http_timeout: Request timeout in seconds (default 120).
        output_dir: Directory for audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "litellm-speech"
    discovery_support = DiscoverySupport.NONE
    """LiteLLM routes to many upstream catalogs and has no single
    authoritative model list, so slugs are accepted provisionally."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(fallback=_FALLBACK)

    def get_capabilities(self) -> ProviderCapabilities:
        """Speech generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=list(dict.fromkeys(_FORMAT_TO_MIME.values())),
        )

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        http_timeout: float = 120.0,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ):
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self._api_key = api_key
        self._api_base = api_base
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate speech audio via ``litellm.speech``."""
        litellm = import_litellm()
        payload = self.prepare_payload(step)
        text = payload.pop("prompt", step.prompt or "")
        voice = payload.pop("voice", "alloy")
        response_format = str(payload.pop("response_format", "mp3")).lower()
        media_type = _FORMAT_TO_MIME.get(response_format, "audio/mpeg")

        try:
            response = litellm.speech(
                model=step.model,
                input=text,
                voice=voice,
                response_format=response_format,
                timeout=self._http_timeout,
                drop_params=True,
                **credential_kwargs(self._api_key, self._api_base),
                **payload,
            )
        except Exception as exc:
            raise ProviderError(
                f"LiteLLM speech generation failed: {exc}",
                error_code=map_litellm_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

        data = response.content
        if not data:
            raise ProviderError(f"LiteLLM returned no audio for model {step.model!r}")
        suffix = f".{response_format}"
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}{suffix}"
        else:
            fd, tmp = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            out_path = Path(tmp)
        out_path.write_bytes(data)

        asset = Asset(
            url=local_file_url(out_path.resolve()),
            media_type=media_type,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        asset.metadata["audio_type"] = "speech"
        asset.audio = AudioMetadata(channels=1, codec=response_format)
        duration = probe_audio_duration(out_path)
        if duration is not None:
            asset.duration = duration
        step.assets.append(asset)

        cost = response_cost(response)
        if cost is not None:
            step.cost_usd = cost
        self._apply_registry_pricing(step)
        return step
