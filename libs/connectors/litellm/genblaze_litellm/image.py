"""LiteLLMImageProvider — image generation through the LiteLLM SDK.

One adapter for every image model LiteLLM routes to: OpenAI ``gpt-image-*``
and ``dall-e-*``, Azure, Vertex AI Imagen, Gemini, Bedrock (Stability, Titan,
Nova Canvas), Recraft, xAI, and more. Models use LiteLLM ids such as
``vertex_ai/imagen-4.0-generate-001`` or ``bedrock/stability.sd3-5-large-v1:0``.

Synchronous API: ``litellm.image_generation`` returns base64 image data or a
CDN URL per image.

Docs: https://docs.litellm.ai/docs/image_generation
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
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
from genblaze_core.providers.base import validate_asset_url
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_litellm._common import credential_kwargs, import_litellm, jsonable, response_cost
from genblaze_litellm._errors import map_litellm_error

_FORMAT_TO_MEDIA = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}

_FALLBACK = ModelSpec(model_id="*", modality=Modality.IMAGE)


class LiteLLMImageProvider(SyncProvider):
    """Provider adapter for image generation via LiteLLM.

    ``step.params`` (``size``, ``quality``, ``n``, ``style``, ``output_format``,
    provider-specific keys) are forwarded to ``litellm.image_generation`` with
    ``drop_params=True``, so a param a provider does not accept is dropped
    instead of failing the step.

    Base64 results are written to a local file and hashed; URL results are
    SSRF-validated and kept as the asset URL for a storage sink to transfer.
    ``step.cost_usd`` is set from LiteLLM's per-response cost when LiteLLM has
    a price for the model.

    Args:
        api_key: Provider API key. When omitted, LiteLLM reads the provider's
            own env vars (``OPENAI_API_KEY``, AWS credentials, ...).
        api_base: Custom endpoint (Azure deployment, self-hosted server).
        http_timeout: Request timeout in seconds (default 120).
        output_dir: Directory for decoded images (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "litellm-image"
    discovery_support = DiscoverySupport.NONE
    """LiteLLM routes to many upstream catalogs and has no single
    authoritative model list, so slugs are accepted provisionally."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(fallback=_FALLBACK)

    def get_capabilities(self) -> ProviderCapabilities:
        """Image generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["image/png", "image/jpeg", "image/webp"],
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

    def _persist_image_bytes(self, data: bytes, step: Step, index: int, ext: str) -> Path:
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}_{index}{ext}"
        else:
            fd, tmp = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            out_path = Path(tmp)
        out_path.write_bytes(data)
        return out_path

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate image(s) via ``litellm.image_generation``."""
        litellm = import_litellm()
        payload = self.prepare_payload(step)
        prompt = payload.pop("prompt", step.prompt or "")
        out_fmt = str(payload.get("output_format", "png")).lower()
        media_type = _FORMAT_TO_MEDIA.get(out_fmt, "image/png")
        ext = ".jpg" if media_type == "image/jpeg" else f".{media_type.split('/')[1]}"

        try:
            response = litellm.image_generation(
                prompt=prompt,
                model=step.model,
                timeout=self._http_timeout,
                drop_params=True,
                **credential_kwargs(self._api_key, self._api_base),
                **payload,
            )
        except Exception as exc:
            raise ProviderError(
                f"LiteLLM image generation failed: {exc}",
                error_code=map_litellm_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

        for i, img in enumerate(getattr(response, "data", None) or []):
            b64 = getattr(img, "b64_json", None)
            remote_url = getattr(img, "url", None)
            if b64:
                data = base64.b64decode(b64)
                out_path = self._persist_image_bytes(data, step, i, ext)
                asset = Asset(
                    url=local_file_url(out_path.resolve()),
                    media_type=media_type,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                )
            elif remote_url:
                validate_asset_url(remote_url)
                asset = Asset(url=remote_url, media_type=media_type)
            else:
                continue
            revised = getattr(img, "revised_prompt", None)
            if revised:
                asset.metadata["revised_prompt"] = revised
            step.assets.append(asset)

        if not step.assets:
            raise ProviderError(f"LiteLLM returned no images for model {step.model!r}")

        usage = getattr(response, "usage", None)
        if usage is not None:
            step.provider_payload["usage"] = jsonable(usage)
        cost = response_cost(response)
        if cost is not None:
            step.cost_usd = cost
        self._apply_registry_pricing(step)
        return step
