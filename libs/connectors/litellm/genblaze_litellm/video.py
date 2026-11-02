"""LiteLLMVideoProvider — video generation through the LiteLLM SDK.

One adapter for the video models LiteLLM routes to: OpenAI Sora, Azure,
Gemini and Vertex AI Veo, RunwayML, and more. Models use LiteLLM ids such as
``openai/sora-2`` or ``gemini/veo-3.0-generate-preview``.

Asynchronous job API:
  ``litellm.video_generation`` → poll ``litellm.video_status`` →
  download ``litellm.video_content``. LiteLLM encodes the upstream provider in
  the returned video id, so status and content calls route back on their own.

Docs: https://docs.litellm.ai/docs/videos
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoverySupport,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
)
from genblaze_core.providers.base import BaseProvider
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_litellm._common import credential_kwargs, import_litellm, jsonable, response_cost
from genblaze_litellm._errors import map_litellm_error

_TERMINAL_STATUSES = frozenset({"completed", "failed"})

_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    # Standard ``duration`` (seconds) → the Videos API ``seconds`` field.
    param_aliases={"duration": "seconds"},
)


class LiteLLMVideoProvider(BaseProvider):
    """Provider adapter for video generation via LiteLLM.

    ``seconds`` (or ``duration``), ``size`` and provider-specific keys are
    forwarded to ``litellm.video_generation``. Submission is a single attempt
    so an ambiguous failure never bills twice; the finished video is
    downloaded with ``litellm.video_content`` and written to a local file.

    Args:
        api_key: Provider API key. When omitted, LiteLLM reads the provider's
            own env vars.
        api_base: Custom endpoint (Azure deployment, self-hosted server).
        poll_interval: Seconds between status checks (default 5).
        http_timeout: Request timeout in seconds (default 120).
        output_dir: Directory for downloaded videos (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "litellm-video"
    discovery_support = DiscoverySupport.NONE
    """LiteLLM routes to many upstream catalogs and has no single
    authoritative model list, so slugs are accepted provisionally."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(fallback=_FALLBACK)

    def get_capabilities(self) -> ProviderCapabilities:
        """Video generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        poll_interval: float = 5.0,
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
        self.poll_interval = poll_interval
        self._api_key = api_key
        self._api_base = api_base
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None

    def _error(self, action: str, exc: Exception) -> ProviderError:
        return ProviderError(
            f"LiteLLM video {action} failed: {exc}",
            error_code=map_litellm_error(exc),
            retry_after=retry_after_from_response(exc),
        )

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create a video generation job via ``litellm.video_generation``."""
        litellm = import_litellm()
        payload = self.prepare_payload(step)
        prompt = payload.pop("prompt", step.prompt or "")
        if "seconds" in payload:
            # The Videos API takes seconds as a string ("4", "8", "12").
            payload["seconds"] = str(payload["seconds"])
        try:
            video = litellm.video_generation(
                prompt=prompt,
                model=step.model,
                timeout=self._http_timeout,
                drop_params=True,
                **credential_kwargs(self._api_key, self._api_base),
                **payload,
            )
        except Exception as exc:
            raise self._error("submit", exc) from exc
        if not getattr(video, "id", None):
            raise ProviderError(f"LiteLLM returned no video id for model {step.model!r}")
        return video.id

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check job status via ``litellm.video_status``."""
        litellm = import_litellm()
        try:
            video = litellm.video_status(
                video_id=prediction_id,
                timeout=self._http_timeout,
                **credential_kwargs(self._api_key, self._api_base),
            )
        except Exception as exc:
            raise self._error("poll", exc) from exc
        if getattr(video, "status", None) in _TERMINAL_STATUSES:
            self._cache_poll_result(prediction_id, video)
            return True
        return False

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Download the finished video via ``litellm.video_content``."""
        litellm = import_litellm()
        creds = credential_kwargs(self._api_key, self._api_base)
        try:
            video = self._get_cached_poll_result(prediction_id)
            if video is None:
                video = litellm.video_status(
                    video_id=prediction_id, timeout=self._http_timeout, **creds
                )

            step.provider_payload["litellm"] = {
                "video_id": video.id,
                "model": getattr(video, "model", None),
                "status": video.status,
                "seconds": getattr(video, "seconds", None),
                "size": getattr(video, "size", None),
            }
            usage = getattr(video, "usage", None)
            if usage:
                step.provider_payload["usage"] = jsonable(usage)

            if video.status == "failed":
                error = getattr(video, "error", None)
                message = (
                    (error.get("message") if isinstance(error, dict) else str(error))
                    if error
                    else "Video generation failed"
                )
                raise ProviderError(str(message), error_code=ProviderErrorCode.MODEL_ERROR)

            data = litellm.video_content(
                video_id=prediction_id, timeout=self._http_timeout, **creds
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise self._error("fetch_output", exc) from exc

        if not data:
            raise ProviderError(f"LiteLLM returned an empty video for job {prediction_id!r}")
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}.mp4"
        else:
            fd, tmp = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            out_path = Path(tmp)
        out_path.write_bytes(data)

        asset = Asset(
            url=local_file_url(out_path.resolve()),
            media_type="video/mp4",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        asset.video = VideoMetadata()
        seconds = getattr(video, "seconds", None)
        if seconds:
            try:
                asset.duration = float(seconds)
            except (TypeError, ValueError):
                pass
        step.assets.append(asset)

        cost = response_cost(video)
        if cost is not None:
            step.cost_usd = cost
        self._apply_registry_pricing(step)
        return step
