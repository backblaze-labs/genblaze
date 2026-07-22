"""ElevenLabsTTSProvider — adapter for ElevenLabs Text-to-Speech API.

Synchronous API: returns audio bytes directly.

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships a
pattern-keyed ``ModelFamily`` plus ``DiscoverySupport.NATIVE`` discovery
via ``client.models.get_all()``. The ElevenLabs catalog is small,
authoritative, and changes on the vendor's release cycle — discovery
gives users pre-flight ``NOT_FOUND`` for retired model ids without
shipping a static slug list in the SDK.

**Pricing**: previously hardcoded as per-1K-character rates per model
tier. As of 0.3.0 the SDK no longer ships pricing — see
``docs/reference/pricing-recipes.md`` for the canonical recipe.

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, WordTiming
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoveryResult,
    DiscoverySupport,
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

from genblaze_elevenlabs._errors import map_elevenlabs_error

logger = logging.getLogger("genblaze.elevenlabs.tts")

_FORMAT_TO_MIME = {
    "mp3_44100_128": "audio/mpeg",
    "mp3_44100_192": "audio/mpeg",
    "mp3_44100_64": "audio/mpeg",
    "mp3_44100_32": "audio/mpeg",
    "mp3_22050_32": "audio/mpeg",
    "pcm_16000": "audio/pcm",
    "pcm_22050": "audio/pcm",
    "pcm_24000": "audio/pcm",
    "pcm_44100": "audio/pcm",
    "wav_44100": "audio/wav",
    "opus_48000_128": "audio/opus",
}

_FORMAT_TO_EXT = {
    "audio/mpeg": ".mp3",
    "audio/pcm": ".pcm",
    "audio/wav": ".wav",
    "audio/opus": ".opus",
}

# The ElevenLabs TTS family covers every ``eleven_*`` model id —
# eleven_v3, eleven_multilingual_v2, eleven_flash_v2_5,
# eleven_turbo_v2_5, and any future variant. The wire shape (text input,
# voice_id parameter, output_format selection) is uniform across tiers,
# so a single family is sufficient. Discovery via client.models.get_all()
# upgrades family-matched slugs to OK_AUTHORITATIVE iff present in the
# live catalog.
_ELEVENLABS_TTS_FAMILY = ModelFamily(
    name="elevenlabs-tts",
    pattern=re.compile(r"^eleven_"),
    spec_template=ModelSpec(model_id="*", modality=Modality.AUDIO),
    description="ElevenLabs TTS family — all eleven_* model variants.",
    example_slugs=(
        "eleven_v3",
        "eleven_multilingual_v2",
        "eleven_flash_v2_5",
        "eleven_turbo_v2_5",
    ),
)


_FALLBACK = ModelSpec(model_id="*", modality=Modality.AUDIO)


def _parse_elevenlabs_alignment(
    chars: list[str],
    starts: list[float],
    ends: list[float],
) -> list[WordTiming]:
    """Build WordTiming list from ElevenLabs character-level alignment.

    Groups consecutive characters into words (split on spaces) and uses the
    first character's start and last character's end as word boundaries.
    """
    timings: list[WordTiming] = []
    current_word = ""
    word_start: float | None = None
    word_end: float = 0.0

    for ch, s, e in zip(chars, starts, ends, strict=False):
        if ch == " ":
            if current_word:
                wt = WordTiming(word=current_word, start=word_start or 0.0, end=word_end)
                timings.append(wt)
                current_word = ""
                word_start = None
        else:
            if word_start is None:
                word_start = s
            current_word += ch
            word_end = e

    # Flush last word
    if current_word:
        timings.append(WordTiming(word=current_word, start=word_start or 0.0, end=word_end))

    return timings


class ElevenLabsTTSProvider(SyncProvider):
    """Provider adapter for ElevenLabs Text-to-Speech.

    Models match the ``elevenlabs-tts`` family — any ``^eleven_`` slug.

    Args:
        api_key: ElevenLabs API key. Falls back to ELEVENLABS_API_KEY env var.
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL (no-op for NATIVE
            but accepted for API uniformity with PARTIAL siblings).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "elevenlabs-tts"
    discovery_support = DiscoverySupport.NATIVE
    """ElevenLabs exposes ``client.models.get_all()`` as an authoritative
    catalog endpoint. Discovery cache populated lazily on first
    ``validate_model`` / ``discover_models`` call; 1-hour TTL by default."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_ELEVENLABS_TTS_FAMILY,),
            fallback=_FALLBACK,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """ElevenLabs TTS: audio speech generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/pcm", "audio/wav", "audio/opus"],
        )

    def __init__(
        self,
        api_key: str | None = None,
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
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None
        # Wire the discovery cache lazily — fetcher closes over self so it
        # picks up the lazy-initialized client.
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_models,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    # --- catalog discovery (DiscoverySupport.NATIVE) ----------------------

    def _fetch_models(self) -> DiscoveryResult:
        """Fetcher backing ``discover_models`` — calls client.models.get_all().

        ElevenLabs returns a list of Model objects with ``model_id``
        fields; we collect those into a frozenset.
        """
        try:
            client = self._get_client()
            models = client.models.get_all()
            slugs: set[str] = set()
            for m in models:
                mid = getattr(m, "model_id", None)
                if isinstance(mid, str):
                    slugs.add(mid)
            return DiscoveryResult.ok(slugs, source_url="https://api.elevenlabs.io/v1/models")
        except Exception as exc:
            return DiscoveryResult.failed(
                f"ElevenLabs models.get_all() failed: {exc}",
                source_url="https://api.elevenlabs.io/v1/models",
            )

    def discover_models(
        self,
        *,
        max_age_seconds: float | None = ...,  # type: ignore[assignment]
    ) -> DiscoveryResult:
        """Snapshot the ElevenLabs model catalog. Single-flight, TTL-bounded."""
        cache = self._models._discovery_cache
        assert cache is not None  # wired in __init__
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            return cache.get()
        return cache.get(max_age_seconds=max_age_seconds)

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
        """Generate speech audio via ElevenLabs TTS API."""
        client = self._get_client()
        try:
            # Run the spec pipeline; result mostly mirrors step.params since
            # the spec is permissive apart from pricing.
            payload = self.prepare_payload(step)

            voice_id = payload.get("voice_id", "JBFqnCBsd6RMkjVDRZzb")
            output_format = payload.get("output_format", "mp3_44100_128")
            media_type = _FORMAT_TO_MIME.get(output_format, "audio/mpeg")
            ext = _FORMAT_TO_EXT.get(media_type, ".mp3")

            kwargs: dict = {
                "text": payload.get("prompt", step.prompt or ""),
                "voice_id": voice_id,
                "model_id": step.model,
                "output_format": output_format,
            }

            voice_settings: dict = {}
            if "stability" in payload:
                voice_settings["stability"] = float(payload["stability"])
            if "similarity_boost" in payload:
                voice_settings["similarity_boost"] = float(payload["similarity_boost"])
            if "style" in payload:
                voice_settings["style"] = float(payload["style"])
            if voice_settings:
                kwargs["voice_settings"] = voice_settings

            if "language_code" in payload:
                kwargs["language_code"] = payload["language_code"]
            if step.seed is not None:
                kwargs["seed"] = step.seed

            # Use timestamps endpoint when requested for word-level timing data.
            # This dispatch stays in generate() — distinct response shape.
            word_timings: list[WordTiming] | None = None
            if payload.get("with_timestamps"):
                response = client.text_to_speech.convert_with_timestamps(**kwargs)
                import base64

                # elevenlabs 2.x returns AudioWithTimestampsResponse, a
                # pydantic model — not a dict. The audio field is
                # `audio_base_64` (underscores); the wire alias
                # `audio_base64` only applies to raw JSON, not the parsed
                # object. `alignment` is itself a model (Optional — the API
                # may omit it), not a dict of lists.
                audio_bytes = base64.b64decode(response.audio_base_64)
                alignment = response.alignment
                if alignment is not None:
                    al_chars = alignment.characters
                    starts = alignment.character_start_times_seconds
                    ends = alignment.character_end_times_seconds
                    if al_chars and starts and ends:
                        word_timings = _parse_elevenlabs_alignment(al_chars, starts, ends)
            else:
                # convert() returns an iterator of audio bytes
                audio_iter = client.text_to_speech.convert(**kwargs)
                audio_bytes = b"".join(audio_iter)

            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{ext}"
            else:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(audio_bytes)
            file_url = local_file_url(out_path.resolve())
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "speech"
            asset.size_bytes = len(audio_bytes)
            # Probe actual audio duration (requires mutagen — optional dep)
            from genblaze_core._utils import probe_audio_duration

            dur = probe_audio_duration(out_path)
            if dur is not None:
                asset.duration = dur

            # Populate audio metadata from output_format (e.g. "mp3_44100_128")
            audio_meta: dict[str, Any] = {"channels": 1}
            parts = output_format.split("_")
            if parts:
                audio_meta["codec"] = parts[0]
            if len(parts) >= 2 and parts[1].isdigit():
                audio_meta["sample_rate"] = int(parts[1])
            if len(parts) >= 3 and parts[2].isdigit():
                audio_meta["bitrate"] = int(parts[2]) * 1000  # kbps → bps
            if word_timings:
                audio_meta["word_timings"] = word_timings
            asset.audio = AudioMetadata(**audio_meta)

            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"ElevenLabs TTS failed: {exc}",
                error_code=map_elevenlabs_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
