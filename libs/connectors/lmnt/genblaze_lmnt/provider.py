"""LMNTProvider — adapter for the LMNT Text-to-Speech API.

Synchronous API: returns audio bytes directly.

LMNT has no enumerable model catalog, so this provider declares
``DiscoverySupport.NONE`` and ships an empty registry with a permissive
fallback that matches any ``step.model``. Param-shape rules (canonical
``voice_id``→``voice``, ``output_format``→``format`` aliases) ride on the
fallback ``ModelSpec``.

This connector is the **proof-point** for the catalog-decoupling
architecture in ``genblaze-core 0.3.0`` — LMNT was already empty-defaults
+ permissive-fallback before the migration, and the only change required
was declaring ``discovery_support`` and removing the hardcoded per-char
price (which now lives as a user-registered recipe in
``docs/reference/pricing-recipes.md``).

**Pricing**: LMNT was previously hardcoded at ``0.00015`` USD/char on
the fallback spec. As of ``0.3.0`` the SDK no longer ships pricing.
Register it explicitly if you want cost tracking::

    from genblaze_core.providers import per_input_chars
    provider.models.register_pricing(
        "lmnt-1", per_input_chars(0.00015, per=1)
    )

**lmnt SDK 2.6+**: the client is synchronous (``lmnt.Lmnt``) and speech is
generated via ``client.speech.generate_detailed(..., return_timestamps=True)``,
which returns base64-encoded audio + optional word-level timestamps in one
JSON response (the closest 2.x equivalent of the old 1.x
``Speech.synthesize()`` dict). lmnt 2.6.0 renamed this endpoint's
``return_durations``/``durations`` surface to ``return_timestamps``/
``timestamps`` (item type ``Duration`` → ``Timestamp``), so the pin floor is
``lmnt>=2.6``. The 1.x SDK's ``speed`` parameter has no
2.x equivalent — LMNT replaced it with ``temperature``/``top_p`` on the
"blizzard" model, which control expressiveness rather than pacing, so
there's no direct pacing knob to forward it to. A ``speed`` step param is
dropped with a warning (naming ``temperature``/``top_p`` as the closest
2.x knobs) rather than silently forwarded.
See https://github.com/backblaze-labs/genblaze/issues/166.

Docs: https://docs.lmnt.com/
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, WordTiming
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

from ._errors import map_lmnt_error

logger = logging.getLogger("genblaze.lmnt")

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "aac": "audio/aac",
}


# Fallback spec — LMNT has no enumerable model list. Any ``step.model``
# matches and inherits the canonical-to-native aliasing. Pricing is
# user-registered (see module docstring).
_LMNT_FALLBACK_SPEC = ModelSpec(
    model_id="*",
    modality=Modality.AUDIO,
    param_aliases={"voice_id": "voice", "output_format": "format"},
)


class LMNTProvider(SyncProvider):
    """Provider adapter for LMNT Text-to-Speech.

    Ultra-low latency TTS with natural-sounding voices. The registry uses
    a permissive fallback spec (no enumerated models) so every LMNT model
    id passes through with canonical parameter aliasing
    (``voice_id`` → ``voice``, ``output_format`` → ``format``).

    Args:
        api_key: LMNT API key. Falls back to LMNT_API_KEY env var.
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "lmnt"
    discovery_support = DiscoverySupport.NONE
    """LMNT has no ``GET /v1/models`` endpoint. Slug freshness is the
    user's responsibility — pass any model id and the upstream API decides."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(fallback=_LMNT_FALLBACK_SPEC)

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
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ):
        # probe_cache_* are no-ops on this NONE-discovery provider; accepted
        # for API uniformity so calling code can pass them to any provider
        # without TypeError.
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self._api_key = api_key
        self._output_dir = Path(output_dir) if output_dir else None
        self._speech_client: Any = None
        # Warn once per provider, not once per clip: a batch of steps all
        # carrying the removed `speed` param would otherwise log the same
        # notice on every generate() call. Mirrors model_registry's
        # `_warned_deprecated` dedup pattern.
        self._warned_speed = False

    def _make_client(self):
        """Create a fresh LMNT client for a single generate() call."""
        try:
            from lmnt import Lmnt
        except ImportError as exc:
            raise ProviderError("lmnt package not installed. Run: pip install lmnt") from exc
        kwargs: dict = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return Lmnt(**kwargs)

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate speech audio via LMNT TTS API."""
        # Per-call client avoids shared-state issues across concurrent steps.
        # _speech_client is checked first to support test mocks.
        client = self._speech_client if self._speech_client is not None else self._make_client()
        owns_client = self._speech_client is None
        try:
            # Run the spec pipeline — rewrites voice_id→voice, output_format→format.
            payload = self.prepare_payload(step)

            voice_id = payload.get("voice", "lily")
            output_format = payload.get("format", "mp3")
            media_type = _FORMAT_TO_MIME.get(output_format, "audio/mpeg")
            ext = f".{output_format}"

            generate_kwargs: dict = {
                "voice": voice_id,
                "text": payload.get("prompt", step.prompt or ""),
                "return_timestamps": True,
            }

            if "format" in payload:
                generate_kwargs["format"] = payload["format"]
            if "speed" in payload and not self._warned_speed:
                # lmnt 2.x dropped `speed` in favor of temperature/top_p, which
                # control expressiveness rather than pacing — there's no
                # equivalent to forward, so warn instead of silently dropping.
                # Emitted once per provider to avoid per-clip log spam.
                self._warned_speed = True
                logger.warning(
                    "LMNT provider: 'speed' is not supported by lmnt SDK 2.x "
                    "and will be ignored; 2.x exposes 'temperature'/'top_p' "
                    "for expressiveness (no direct pacing equivalent). "
                    "See issue #166."
                )
            if "language" in payload:
                generate_kwargs["language"] = payload["language"]
            if step.seed is not None:
                generate_kwargs["seed"] = step.seed

            # generate_detailed() is the JSON-response counterpart to the
            # raw-bytes speech.generate() — it's the only endpoint that can
            # also return word-level timestamps (return_timestamps=True),
            # matching the shape the old 1.x synthesize() call returned.
            result = client.speech.generate_detailed(**generate_kwargs)
            audio_bytes = base64.b64decode(result.audio)

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

            audio_meta_kwargs: dict[str, Any] = {"channels": 1, "codec": output_format}

            # Convert LMNT timestamps (list of ``lmnt.types.Timestamp`` pydantic
            # models) into typed WordTiming objects.
            timestamps = result.timestamps
            if timestamps:
                word_timings = [
                    WordTiming(word=t.text, start=t.start, end=t.start + t.duration)
                    for t in timestamps
                ]
                audio_meta_kwargs["word_timings"] = word_timings
                # Compute duration from word timings
                if word_timings:
                    asset.duration = max(wt.end for wt in word_timings)
                step.provider_payload = {
                    "lmnt": {"timestamps": [t.model_dump() for t in timestamps]}
                }

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
            # Close per-call client only (don't close injected test clients).
            # lmnt 2.x's Lmnt.close() is synchronous (sync httpx.Client).
            if owns_client:
                try:
                    client.close()
                except Exception:  # noqa: S110
                    pass
