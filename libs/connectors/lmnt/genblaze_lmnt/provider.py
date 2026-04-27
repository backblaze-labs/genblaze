"""LMNTProvider — adapter for the LMNT Text-to-Speech API.

Synchronous API: returns audio bytes directly.

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. LMNT has no enumerated model catalogue
so the registry ships a permissive fallback (``FALLBACK_SPEC`` with pricing)
that matches any ``step.model`` and applies per-character pricing.

Docs: https://docs.lmnt.com/
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core._utils import _run_async
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, WordTiming
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
    per_input_chars,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_lmnt_error

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "aac": "audio/aac",
}

# Per-character pricing (USD)
_PRICE_PER_CHAR = 0.00015


# Fallback spec — LMNT has no enumerable model list; any model_id applies
# the per-character pricing and canonical-to-native aliasing.
_LMNT_FALLBACK_SPEC = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    pricing=per_input_chars(_PRICE_PER_CHAR, per=1),
    param_aliases={"voice_id": "voice", "output_format": "format"},
    param_coercers={"speed": float},
)


class LMNTProvider(SyncProvider):
    """Provider adapter for LMNT Text-to-Speech.

    Ultra-low latency TTS with natural-sounding voices. The registry uses
    a permissive fallback spec (no enumerated models) so every LMNT model
    id passes through with per-character pricing and canonical parameter
    aliasing (``voice_id`` → ``voice``, ``output_format`` → ``format``).

    Args:
        api_key: LMNT API key. Falls back to LMNT_API_KEY env var.
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "lmnt"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(defaults={}, fallback=_LMNT_FALLBACK_SPEC)

    def get_capabilities(self) -> ProviderCapabilities:
        """LMNT: low-latency text-to-speech generation."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            output_formats=["audio/mpeg", "audio/wav", "audio/aac"],
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
        self._speech_client: Any = None

    def _make_client(self):
        """Create a fresh LMNT Speech client for a single generate() call."""
        try:
            from lmnt.api import Speech
        except ImportError as exc:
            raise ProviderError("lmnt package not installed. Run: pip install lmnt") from exc
        kwargs: dict = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return Speech(**kwargs)

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate speech audio via LMNT TTS API."""
        # Per-call client avoids shared-state issues across concurrent steps.
        # _speech_client is checked first to support test mocks.
        client = self._speech_client if self._speech_client is not None else self._make_client()
        owns_client = self._speech_client is None
        try:
            # Run the spec pipeline — rewrites voice_id→voice, output_format→format,
            # and coerces speed to float.
            payload = self.prepare_payload(step)

            voice_id = payload.get("voice", "lily")
            output_format = payload.get("format", "mp3")
            media_type = _FORMAT_TO_MIME.get(output_format, "audio/mpeg")
            ext = f".{output_format}"

            synth_kwargs: dict = {
                "voice": voice_id,
                "text": payload.get("prompt", step.prompt or ""),
            }

            if "format" in payload:
                synth_kwargs["format"] = payload["format"]
            if "speed" in payload:
                synth_kwargs["speed"] = payload["speed"]
            if "language" in payload:
                synth_kwargs["language"] = payload["language"]
            if step.seed is not None:
                synth_kwargs["seed"] = step.seed

            # LMNT SDK is async — wrap in sync call
            # synthesize() returns {"audio": bytes, "durations": [...]}
            result = _run_async(client.synthesize(**synth_kwargs))
            audio_bytes = result["audio"]

            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{ext}"
            else:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(audio_bytes)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "speech"

            audio_meta_kwargs: dict[str, Any] = {"channels": 1, "codec": output_format}

            # Convert LMNT durations into typed WordTiming objects
            durations = result.get("durations")
            if durations:
                word_timings = [
                    WordTiming(
                        word=d.get("phonemes", d.get("text", d.get("word", ""))) or "",
                        start=d["start"],
                        end=d["start"] + d["duration"] if "duration" in d else d.get("end", 0),
                    )
                    for d in durations
                    if isinstance(d, dict)
                ]
                audio_meta_kwargs["word_timings"] = word_timings
                # Compute duration from word timings
                if word_timings:
                    asset.duration = max(wt.end for wt in word_timings)
                step.provider_payload = {"lmnt": {"durations": durations}}

            asset.audio = AudioMetadata(**audio_meta_kwargs)

            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"LMNT TTS failed: {exc}",
                error_code=map_lmnt_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
        finally:
            # Close per-call client only (don't close injected test clients)
            if owns_client:
                try:
                    _run_async(client.close())
                except Exception:  # noqa: S110
                    pass
