"""NvidiaImageProvider — image generation via NVIDIA NIM.

Most NVIDIA image endpoints (SDXL, SD 3.5, FLUX) return an inline base64
payload synchronously, which fits the ``SyncProvider`` shape. When an endpoint
returns 202 (async), we short-poll NVCF inside ``generate()`` so the caller
still sees a single blocking call.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    ProviderCapabilities,
    SyncProvider,
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
from .models.image import build_image_registry


class NvidiaImageProvider(SyncProvider):
    """Adapter for NVIDIA NIM image generation (SDXL, SD 3.5, FLUX).

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

    name = "nvidia-image"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return build_image_registry()

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
    ) -> None:
        super().__init__(models=models)
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
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["image/png", "image/jpeg"],
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
                f"NVIDIA image generate failed: {exc}",
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
                f"NVIDIA image generate failed ({status}): {detail}",
                error_code=map_nvidia_error(Exception(detail), status),
                retry_after=retry_after_from_response(headers),
            )

        step.provider_payload = {"nvidia": {"status": "succeeded"}}

        urls = extract_asset_urls(body)
        if urls:
            self._attach_url_assets(step, urls)
            return step

        payloads = extract_base64_assets(body)
        if payloads:
            for raw, mime in payloads:
                media_type = mime or "image/png"
                ext = mimetypes.guess_extension(media_type) or ".png"
                url = save_bytes_to_output_dir(
                    raw, self._output_dir, extension=ext, prefix="nvidia-image"
                )
                step.assets.append(Asset(url=url, media_type=media_type))
            return step

        raise ProviderError(
            "NVIDIA image response contained no asset URL or base64 payload",
            error_code=ProviderErrorCode.SERVER_ERROR,
        )

    def _attach_url_assets(self, step: Step, urls: list[str]) -> None:
        """Attach hosted URLs as Assets atomically (validate all before any land)."""
        new_assets: list[Asset] = []
        for url in urls:
            validate_asset_url(url)
            path = urlparse(url).path
            mime, _ = mimetypes.guess_type(path)
            if mime is None or not mime.startswith("image/"):
                mime = "image/png"
            new_assets.append(Asset(url=url, media_type=mime))
        step.assets.extend(new_assets)
