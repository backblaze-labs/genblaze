"""NvidiaVideoProvider — video generation via NVIDIA NIM (build.nvidia.com).

Auth: set ``NVIDIA_API_KEY`` env var or pass ``api_key=`` to the constructor.

NVIDIA's video endpoints (Cosmos, Edify Video) return either:
- ``202 Accepted`` with an ``NVCF-REQID`` header → poll ``api.nvcf.nvidia.com``
- ``200 OK`` with inline base64 or a hosted URL (rare, fast models only)

Both paths converge on the same ``submit → poll → fetch_output`` lifecycle.
Unknown models pass through via the registry's permissive fallback spec.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    SubmitResult,
    validate_asset_url,
)
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._base import (
    NvidiaClient,
    extract_asset_urls,
    extract_base64_assets,
    extract_error_detail,
    save_bytes_to_output_dir,
)
from ._errors import map_nvidia_error
from .models.video import build_video_registry

# NVCF-REQID header value is the async job id. Lowercase lookup since we
# normalize header keys in NvidiaClient.
_NVCF_REQID_HEADER = "nvcf-reqid"

# Marker prediction id for 200-inline submissions — the body is already cached
# so poll() just returns True on first tick.
_INLINE_PREFIX = "nvidia-inline:"


class NvidiaVideoProvider(BaseProvider):
    """Adapter for NVIDIA NIM video generation (Cosmos, Edify Video).

    Args:
        api_key: NVIDIA API key. Falls back to NVIDIA_API_KEY env var.
        poll_interval: Seconds between NVCF status polls (default 10 — video
            jobs take minutes, and NIM's free tier is RPM-gated so a tight
            loop burns the budget for nothing).
        http_timeout: HTTP request timeout in seconds.
        gen_base_url: Override the generation base URL (self-hosted NIM).
        nvcf_status_url: Override the NVCF async-status URL.
        output_dir: Where to save inline base64 payloads. Defaults to CWD.
        http_client: Pre-built httpx.Client for tests / shared pools.
        models: Optional custom ModelRegistry.
    """

    name = "nvidia-video"
    poll_interval = 10.0

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return build_video_registry()

    def __init__(
        self,
        api_key: str | None = None,
        *,
        poll_interval: float = 10.0,
        http_timeout: float = 120.0,
        gen_base_url: str | None = None,
        nvcf_status_url: str | None = None,
        output_dir: Path | str | None = None,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(models=models, retry_policy=retry_policy)
        self.poll_interval = poll_interval
        self._output_dir: Path | None = Path(output_dir) if output_dir is not None else None
        self._client = NvidiaClient(
            api_key=api_key,
            gen_base_url=gen_base_url,
            nvcf_status_url=nvcf_status_url,
            http_timeout=http_timeout,
            http_client=http_client,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image", "video"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def close(self) -> None:
        """Release httpx resources for internally-created clients."""
        self._client.close()

    # --- lifecycle ---

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        try:
            payload = self.prepare_payload(step)
            status, body, headers = self._client.post_generation(step.model, payload)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"NVIDIA video submit failed: {exc}",
                error_code=map_nvidia_error(exc),
            ) from exc

        if status == 202:
            req_id = headers.get(_NVCF_REQID_HEADER)
            if not req_id:
                raise ProviderError(
                    "NVIDIA returned 202 without an NVCF-REQID header — cannot poll",
                    error_code=ProviderErrorCode.SERVER_ERROR,
                )
            return SubmitResult(prediction_id=str(req_id), estimated_seconds=60.0)

        if status == 200:
            # Inline synchronous completion. Stash the body under a synthetic id
            # so poll() returns True on first call and fetch_output reads from
            # the cache rather than hitting NVCF for a job that doesn't exist.
            synthetic_id = f"{_INLINE_PREFIX}{step.step_id}"
            self._cache_poll_result(synthetic_id, body)
            return SubmitResult(prediction_id=synthetic_id, estimated_seconds=0.0)

        detail = extract_error_detail(body)
        raise ProviderError(
            f"NVIDIA video submit failed ({status}): {detail}",
            error_code=map_nvidia_error(Exception(detail), status),
            retry_after=retry_after_from_response(headers),
        )

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        # Synchronous-inline submissions are already cached — short-circuit.
        pid = str(prediction_id)
        if pid.startswith(_INLINE_PREFIX):
            return True
        try:
            status, body, headers = self._client.poll_nvcf(pid)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"NVIDIA video poll failed: {exc}",
                error_code=map_nvidia_error(exc),
            ) from exc

        if status == 200:
            self._cache_poll_result(pid, body)
            return True
        if status == 202:
            return False
        detail = extract_error_detail(body)
        raise ProviderError(
            f"NVIDIA video poll failed ({status}): {detail}",
            error_code=map_nvidia_error(Exception(detail), status),
            retry_after=retry_after_from_response(headers),
        )

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        pid = str(prediction_id)
        body = self._get_cached_poll_result(pid)
        if body is None:
            # Cache miss — re-fetch from NVCF (unless inline, in which case the
            # body was lost and we can't recover).
            if pid.startswith(_INLINE_PREFIX):
                raise ProviderError(
                    "Inline NVIDIA response evicted from cache before fetch_output",
                    error_code=ProviderErrorCode.SERVER_ERROR,
                )
            status, body, headers = self._client.poll_nvcf(pid)
            if status != 200:
                detail = extract_error_detail(body)
                raise ProviderError(
                    f"NVIDIA video fetch failed ({status}): {detail}",
                    error_code=map_nvidia_error(Exception(detail), status),
                    retry_after=retry_after_from_response(headers),
                )

        step.provider_payload = {"nvidia": {"prediction_id": pid, "status": "succeeded"}}

        # Prefer hosted URLs (CDN-delivered, no base64 overhead). Fall back to
        # inline base64. Both paths populate an Asset with VideoMetadata.
        urls = extract_asset_urls(body)
        if urls:
            self._attach_url_assets(step, urls, default_mime="video/mp4")
            return step

        payloads = extract_base64_assets(body)
        if payloads:
            for raw, mime in payloads:
                media_type = mime or "video/mp4"
                ext = mimetypes.guess_extension(media_type) or ".mp4"
                url = save_bytes_to_output_dir(
                    raw, self._output_dir, extension=ext, prefix="nvidia-video"
                )
                asset = Asset(url=url, media_type=media_type)
                asset.video = VideoMetadata(has_audio=False)
                step.assets.append(asset)
            return step

        raise ProviderError(
            "NVIDIA video response contained no asset URL or base64 payload",
            error_code=ProviderErrorCode.SERVER_ERROR,
        )

    def _attach_url_assets(self, step: Step, urls: list[str], *, default_mime: str) -> None:
        """Attach hosted URLs as Assets atomically (validate all before any land)."""
        new_assets: list[Asset] = []
        for url in urls:
            validate_asset_url(url)
            path = urlparse(url).path
            mime, _ = mimetypes.guess_type(path)
            if mime is None or not mime.startswith("video/"):
                mime = default_mime
            asset = Asset(url=url, media_type=mime)
            asset.video = VideoMetadata(has_audio=False)
            new_assets.append(asset)
        step.assets.extend(new_assets)
