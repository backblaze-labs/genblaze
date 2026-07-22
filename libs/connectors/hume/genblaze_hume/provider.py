"""HumeTTSProvider — adapter for the Hume AI Octave Text-to-Speech API.

Synchronous API: ``client.tts.synthesize_json()`` returns the full result
with **base64-encoded** audio (no URLs), so this provider decodes the bytes
and writes them to ``output_dir`` (or a tempfile), exposing a ``file://``
asset URL — the same shape as the ElevenLabs / LMNT connectors.

**Catalog architecture (genblaze-core 0.3.0):** Hume exposes no per-model
catalog endpoint — the Octave model is selected via the request's
``version`` field ("1" or "2"), not a slug list. This connector therefore
declares ``DiscoverySupport.NONE`` and ships a single pattern-keyed
``ModelFamily`` for ``octave-*`` slugs (family-matched slugs preflight as
``OK_PROVISIONAL``) plus a permissive fallback so any slug passes through.
The connector maps ``step.model`` → the API ``version`` field.

**Pricing**: Octave TTS bills per character of input text. The SDK ships
zero hardcoded prices — register a recipe at runtime; see
``docs/reference/pricing-recipes.md`` ("Hume" section).

Docs: https://dev.hume.ai/docs/text-to-speech-tts/overview
"""

from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.models.voice import Voice
from genblaze_core.providers import (
    DiscoverySupport,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_hume_error

logger = logging.getLogger("genblaze.hume.tts")

# Octave output format → MIME type → file extension. Hume's ``format``
# request field is discriminated by ``type``: mp3 / wav / pcm.
_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}
_FORMAT_TO_EXT = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/pcm": ".pcm",
}

# ``step.model`` slug → Octave model ``version`` request field. Slugs that
# don't map (custom / future) omit ``version`` and let the API default.
_VERSION_FROM_MODEL = {
    "octave-1": "1",
    "octave-2": "2",
}

# Canonical → native param contracts, shared by the family spec_template and
# the permissive fallback so aliasing is uniform across matched/unmatched
# slugs. ``prepare_payload`` applies these (voice_id→voice, output_format→
# format) and coerces numeric params to float.
_HUME_PARAM_ALIASES = {"voice_id": "voice", "output_format": "format"}
_HUME_PARAM_COERCERS = {"speed": float, "temperature": float, "trailing_silence": float}

# The Hume Octave family covers ``octave-1`` / ``octave-2`` (and any future
# ``octave-*`` line). The wire shape is uniform across versions — only the
# ``version`` request field changes — so a single family suffices.
# spec_template.pricing MUST be None (pricing is user-registered).
_HUME_OCTAVE_FAMILY = ModelFamily(
    name="hume-octave",
    pattern=re.compile(r"^octave-"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.AUDIO,
        param_aliases=_HUME_PARAM_ALIASES,
        param_coercers=_HUME_PARAM_COERCERS,
    ),
    description="Hume Octave TTS family — octave-1 / octave-2.",
    example_slugs=("octave-1", "octave-2"),
)

_HUME_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    param_aliases=_HUME_PARAM_ALIASES,
    param_coercers=_HUME_PARAM_COERCERS,
)


class HumeTTSProvider(SyncProvider):
    """Provider adapter for Hume AI Octave Text-to-Speech.

    Models match the ``hume-octave`` family — any ``^octave-`` slug, mapped
    to the API ``version`` field. The synthesize call is synchronous and
    returns base64 audio, which is decoded to a local ``file://`` asset.

    Args:
        api_key: Hume API key. Falls back to ``HUME_API_KEY`` env var.
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL (no-op for NONE
            discovery but accepted for API uniformity with other providers).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "hume-tts"
    discovery_support = DiscoverySupport.NONE
    """Hume exposes no ``GET /models`` catalog — the Octave model is chosen
    via the request ``version`` field, not a slug list. Family-matched
    ``octave-*`` slugs preflight as ``OK_PROVISIONAL``; everything else
    resolves through the permissive fallback as ``UNKNOWN_PERMISSIVE``."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_HUME_OCTAVE_FAMILY,),
            fallback=_HUME_FALLBACK,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """Hume Octave TTS: audio speech generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/wav", "audio/pcm"],
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

    def _get_client(self):
        if self._client is None:
            # The Hume SDK does not auto-read HUME_API_KEY — resolve it here and
            # fail fast with a clear, correctly-classified error rather than
            # constructing a keyless client that 401s deep inside generate().
            key = self._api_key or os.getenv("HUME_API_KEY")
            if not key:
                raise ProviderError(
                    "No Hume API key found. Pass api_key=... or set HUME_API_KEY.",
                    error_code=ProviderErrorCode.AUTH_FAILURE,
                )
            try:
                from hume import HumeClient
            except ImportError as exc:
                raise ProviderError("hume package not installed. Run: pip install hume") from exc
            self._client = HumeClient(api_key=key)
        return self._client

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate speech audio via the Hume Octave TTS API."""
        client = self._get_client()
        try:
            # Run the spec pipeline — rewrites voice_id→voice,
            # output_format→format, and coerces speed/temperature to float.
            payload = self.prepare_payload(step)

            out_format = payload.get("format", "mp3")
            if out_format not in _FORMAT_TO_MIME:
                raise ProviderError(
                    f"Unsupported output_format {out_format!r}. "
                    f"Must be one of: {', '.join(sorted(_FORMAT_TO_MIME))}.",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            media_type = _FORMAT_TO_MIME[out_format]
            ext = _FORMAT_TO_EXT[media_type]

            # Lazy-import the request models so an absent SDK only fails inside
            # the ImportError path of _get_client (and so tests can mock them).
            # Note: `voice_id` is interpreted as a Hume Voice Library *name*
            # (PostedUtteranceVoiceWithName), not an opaque id — see README.
            from hume.tts import PostedUtterance, PostedUtteranceVoiceWithName

            utt_kwargs: dict[str, Any] = {"text": payload.get("prompt", step.prompt or "")}
            voice = payload.get("voice")
            if voice:
                utt_kwargs["voice"] = PostedUtteranceVoiceWithName(name=voice, provider="HUME_AI")
            if "description" in payload:
                utt_kwargs["description"] = payload["description"]
            if "speed" in payload:
                utt_kwargs["speed"] = payload["speed"]
            if "trailing_silence" in payload:
                utt_kwargs["trailing_silence"] = payload["trailing_silence"]

            synth_kwargs: dict[str, Any] = {
                "utterances": [PostedUtterance(**utt_kwargs)],
                "format": {"type": out_format},
                "num_generations": 1,
            }
            version = _VERSION_FROM_MODEL.get(step.model or "")
            if version is not None:
                synth_kwargs["version"] = version
            if "temperature" in payload:
                synth_kwargs["temperature"] = payload["temperature"]

            response = client.tts.synthesize_json(**synth_kwargs)
            generation = response.generations[0]
            audio_bytes = base64.b64decode(generation.audio)

            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{ext}"
            else:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(audio_bytes)
            # Local file output — no validate_asset_url() (it only permits
            # HTTPS; file:// is the documented local-provider convention).
            file_url = local_file_url(out_path.resolve())
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "speech"
            asset.size_bytes = len(audio_bytes)

            duration = getattr(generation, "duration", None)
            if duration is not None:
                asset.duration = float(duration)

            audio_meta: dict[str, Any] = {"channels": 1, "codec": out_format}
            encoding = getattr(generation, "encoding", None)
            if encoding is not None:
                sample_rate = getattr(encoding, "sample_rate", None)
                if sample_rate:
                    audio_meta["sample_rate"] = int(sample_rate)
            asset.audio = AudioMetadata(**audio_meta)

            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Hume TTS failed: {exc}",
                error_code=map_hume_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def list_voices(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> list[Voice]:
        """Return voices from Hume's Voice Library (best-effort, live fetch).

        Degrades to an empty list if the SDK call fails or the catalog can't
        be read — callers treat an empty list as "no picker available".

        Hume's voice catalog (``ReturnVoice``) exposes ``id`` / ``name`` /
        ``provider`` and a ``compatible_octave_models`` list; it carries no
        per-voice language tag, so the ``language`` filter cannot be honored
        here and is accepted only for interface parity with the base hook.
        """
        try:
            client = self._get_client()
            raw = client.tts.voices.list(provider="HUME_AI")
            voices: list[Voice] = []
            for v in raw:
                vid = getattr(v, "id", None) or getattr(v, "name", None)
                if not vid:
                    continue
                if model is not None:
                    compatible = getattr(v, "compatible_octave_models", None)
                    if compatible and model not in compatible:
                        continue
                vname = getattr(v, "name", None) or vid
                voices.append(Voice(voice_id=str(vid), name=str(vname), provider=self.name))
            return voices
        except Exception as exc:  # noqa: BLE001 — advisory hook, never fatal
            logger.debug("Hume list_voices failed: %s", exc)
            return []
