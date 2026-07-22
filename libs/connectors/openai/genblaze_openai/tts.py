"""OpenAITTSProvider — adapter for the OpenAI Text-to-Speech API.

Synchronous API: POST /v1/audio/speech returns audio bytes directly.

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships a
pattern-keyed ``ModelFamily`` plus ``DiscoverySupport.NATIVE`` discovery
via ``client.models.list()`` (filtered to TTS-shaped slugs).

**Pricing**: previously hardcoded as per-1M-character rates per tier
(tts-1: $15/M, tts-1-hd: $30/M, gpt-4o-mini-tts: $12/M). As of 0.3.0
the SDK no longer ships pricing — see ``docs/reference/pricing-recipes.md``
for the canonical recipe.

Docs: https://platform.openai.com/docs/api-reference/audio/createSpeech
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoveryResult,
    DiscoverySupport,
    EnumSchema,
    FloatSchema,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.discovery import DEFAULT_TTL_SECONDS, _DiscoveryCache
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

# OpenAI TTS family — covers tts-1, tts-1-hd, gpt-4o-mini-tts, and any
# future TTS-named slug (gpt-4o-tts, gpt-5-mini-tts, etc.). The pattern
# matches both the legacy ``tts-`` prefix and the ``gpt-*-tts`` shape
# OpenAI's mid-2025 audio models adopted. Voice / response_format /
# speed validation rides on the family spec_template.
_OPENAI_TTS_FAMILY = ModelFamily(
    name="openai-tts",
    pattern=re.compile(r"^(?:tts-|gpt-.+-tts)"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        param_coercers={"speed": float},
        param_schemas={
            "voice": EnumSchema(values=_VALID_VOICES),
            "response_format": EnumSchema(values=_VALID_RESPONSE_FORMATS),
            "speed": FloatSchema(min=0.25, max=4.0),
        },
    ),
    description="OpenAI TTS family — tts-* and gpt-*-tts variants.",
    example_slugs=("tts-1", "tts-1-hd", "gpt-4o-mini-tts"),
)


_FALLBACK = ModelSpec(model_id="*", modality=Modality.AUDIO)


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
    discovery_support = DiscoverySupport.NATIVE
    """OpenAI exposes ``client.models.list()`` as the authoritative
    catalog. The fetcher filters to TTS-shaped slugs so chat / image /
    Sora slugs don't pollute the cache."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_OPENAI_TTS_FAMILY,),
            fallback=_FALLBACK,
        )

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
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None
        # Wire NATIVE discovery — fetcher closes over self.
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_models,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    # --- catalog discovery (DiscoverySupport.NATIVE) ----------------------

    def _fetch_models(self) -> DiscoveryResult:
        """Fetch /v1/models, filter to TTS-shaped slugs."""
        try:
            client = self._get_client()
            response = client.models.list()
            slugs: set[str] = set()
            for model in response.data:
                mid = getattr(model, "id", None)
                if isinstance(mid, str) and self._models.match_family(mid) is not None:
                    slugs.add(mid)
            return DiscoveryResult.ok(slugs, source_url="https://api.openai.com/v1/models")
        except Exception as exc:
            return DiscoveryResult.failed(
                f"OpenAI models.list() failed: {exc}",
                source_url="https://api.openai.com/v1/models",
            )

    def discover_models(
        self,
        *,
        max_age_seconds: float | None = ...,  # type: ignore[assignment]
    ) -> DiscoveryResult:
        """Snapshot the TTS-filtered OpenAI catalog. Single-flight, TTL-bounded."""
        cache = self._models._discovery_cache
        assert cache is not None  # wired in __init__
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            return cache.get()
        return cache.get(max_age_seconds=max_age_seconds)

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
            file_url = local_file_url(out_path.resolve())
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
