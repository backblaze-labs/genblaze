"""StabilityAudioProvider — adapter for Stability AI Stable Audio API.

Synchronous API: POST multipart form, returns audio bytes directly.

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Pricing is per-second on the generated
audio (USD), reading from ``Asset.duration`` first and falling back to the
requested ``step.params["duration"]`` when the audio probe yields no value
(e.g. fake fixtures).

Docs: https://platform.stability.ai/docs/api-reference
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
    EnumSchema,
    FloatSchema,
    ModelRegistry,
    ModelSpec,
    PricingContext,
    PricingStrategy,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_stability_audio_error

_API_URL = "https://api.stability.ai/v2beta/audio/stable-audio-2/text-to-audio"

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
}
_OUTPUT_FORMATS = frozenset(_FORMAT_TO_MIME)

# Per-second pricing for generated audio (USD)
_PRICE_PER_SEC = 0.01


def _per_second_with_param_fallback(rate: float) -> PricingStrategy:
    """Per-second pricing using probed asset duration, falling back to params.

    The standard ``per_output_second`` helper only reads ``Asset.duration``;
    the connector also wants to bill from the requested duration when the
    audio probe returns no value (e.g. test fixtures with fake bytes). This
    bespoke strategy preserves that behavior.
    """

    def _strategy(ctx: PricingContext) -> float | None:
        dur = ctx.output_duration_s
        if dur is None:
            raw = ctx.step.params.get("duration")
            try:
                dur = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                dur = None
        if dur is None:
            return None
        return dur * rate

    return _strategy


def _stable_audio_spec(model_id: str) -> ModelSpec:
    """Single-model spec for Stable Audio 2.5."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.AUDIO,
        pricing=_per_second_with_param_fallback(_PRICE_PER_SEC),
        # Duration arrives as either a numeric or a stringy form ("30"); coerce
        # before the FloatSchema bounds-check runs.
        param_coercers={"duration": float},
        param_schemas={
            "output_format": EnumSchema(values=_OUTPUT_FORMATS),
            "duration": FloatSchema(min=0.5, max=190.0),
        },
    )


class StabilityAudioProvider(SyncProvider):
    """Provider adapter for Stability AI Stable Audio generation.

    Model: ``stable-audio-2.5`` — generates music and sound effects up to 3 min.

    Uses raw HTTP (no SDK) since Stability has no official Python SDK for audio.

    Args:
        api_key: Stability AI API key. Falls back to STABILITY_API_KEY env var.
        http_timeout: HTTP request timeout in seconds (default 120).
        output_dir: Directory for output audio files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "stability-audio"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(defaults={"stable-audio-2.5": _stable_audio_spec("stable-audio-2.5")})

    def get_capabilities(self) -> ProviderCapabilities:
        """Stability Audio: music and sound effect generation from text."""
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            supported_inputs=["text"],
            max_duration=190.0,
            models=self._models.known(),
            output_formats=["audio/mpeg", "audio/wav", "audio/ogg"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        http_timeout: float = 120.0,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(models=models, retry_policy=retry_policy)
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._http_client: Any = None

    def _get_http_client(self):
        if self._http_client is None:
            try:
                import httpx
            except ImportError as exc:
                raise ProviderError("httpx package not installed. Run: pip install httpx") from exc
            self._http_client = httpx.Client(timeout=self._http_timeout)
        return self._http_client

    def close(self) -> None:
        """Close the HTTP client and release connection pool resources."""
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        key = os.environ.get("STABILITY_API_KEY")
        if not key:
            raise ProviderError(
                "No API key. Set STABILITY_API_KEY env var or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return key

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate audio via Stability AI Stable Audio API."""
        client = self._get_http_client()
        api_key = self._get_api_key()
        try:
            # Preserve the bespoke "Invalid duration" wording the tests assert.
            if "duration" in step.params:
                try:
                    dur = float(step.params["duration"])
                except (TypeError, ValueError) as exc:
                    raise ProviderError(
                        f"Invalid duration={step.params['duration']!r}. Must be 0.5–190 seconds.",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    ) from exc
                if dur < 0.5 or dur > 190:
                    raise ProviderError(
                        f"Invalid duration={dur}. Must be 0.5–190 seconds.",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )

            payload = self.prepare_payload(step)
            output_format = payload.get("output_format", "mp3")
            media_type = _FORMAT_TO_MIME.get(output_format, "audio/mpeg")

            form_data: dict = {
                "prompt": payload.get("prompt", step.prompt or ""),
                "output_format": output_format,
            }
            if "duration" in payload:
                form_data["duration"] = str(float(payload["duration"]))
            if step.seed is not None:
                form_data["seed"] = str(step.seed)

            response = client.post(
                _API_URL,
                data=form_data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "audio/*",
                },
            )

            if response.status_code != 200:
                raise ProviderError(
                    f"Stability Audio API error {response.status_code}: {response.text[:200]}",
                    error_code=map_stability_audio_error(Exception(), response.status_code),
                    retry_after=retry_after_from_response(response),
                )

            ext = f".{output_format}"
            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}{ext}"
            else:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(response.content)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type=media_type)
            asset.metadata["audio_type"] = "music"
            asset.size_bytes = len(response.content)
            # Stable Audio outputs stereo audio
            asset.audio = AudioMetadata(channels=2, codec=output_format)

            # Probe actual audio duration, fall back to requested duration
            from genblaze_core._utils import probe_audio_duration

            actual_dur = probe_audio_duration(out_path)
            if actual_dur is not None:
                asset.duration = actual_dur
            elif "duration" in step.params:
                asset.duration = float(step.params["duration"])

            step.assets.append(asset)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Stability Audio generation failed: {exc}",
                error_code=map_stability_audio_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
