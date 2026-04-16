"""DalleProvider — adapter for the OpenAI Images API (DALL-E / gpt-image-1).

Synchronous API: POST /v1/images/generations returns image URLs directly.
gpt-image-1 returns base64 data (no URL support), so we decode and save locally.

Docs: https://platform.openai.com/docs/api-reference/images/create
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider, validate_asset_url
from genblaze_core.runnable.config import RunnableConfig

from genblaze_openai._errors import map_openai_error

logger = logging.getLogger("genblaze.openai.dalle")

# Valid sizes per model
_DALLE3_SIZES = {"1024x1024", "1792x1024", "1024x1792"}
_GPT_IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
_DALLE2_SIZES = {"256x256", "512x512", "1024x1024"}

_VALID_QUALITIES = {"standard", "hd", "low", "medium", "high", "auto"}

# Models that only support b64_json response format (no URL)
_B64_ONLY_MODELS = {"gpt-image-1"}

# Size validation lookup by model family
_SIZE_BY_MODEL: dict[str, set[str]] = {
    "dall-e-2": _DALLE2_SIZES,
    "dall-e-3": _DALLE3_SIZES,
    "gpt-image-1": _GPT_IMAGE_SIZES,
}

# Per-image pricing by (model, quality, size). Prices in USD.
_PRICING: dict[tuple[str, str, str], float] = {
    # DALL-E 3
    ("dall-e-3", "standard", "1024x1024"): 0.040,
    ("dall-e-3", "standard", "1024x1792"): 0.080,
    ("dall-e-3", "standard", "1792x1024"): 0.080,
    ("dall-e-3", "hd", "1024x1024"): 0.080,
    ("dall-e-3", "hd", "1024x1792"): 0.120,
    ("dall-e-3", "hd", "1792x1024"): 0.120,
    # DALL-E 2
    ("dall-e-2", "standard", "256x256"): 0.016,
    ("dall-e-2", "standard", "512x512"): 0.018,
    ("dall-e-2", "standard", "1024x1024"): 0.020,
    # gpt-image-1
    ("gpt-image-1", "low", "1024x1024"): 0.011,
    ("gpt-image-1", "low", "1024x1536"): 0.016,
    ("gpt-image-1", "low", "1536x1024"): 0.016,
    ("gpt-image-1", "low", "auto"): 0.011,
    ("gpt-image-1", "medium", "1024x1024"): 0.042,
    ("gpt-image-1", "medium", "1024x1536"): 0.063,
    ("gpt-image-1", "medium", "1536x1024"): 0.063,
    ("gpt-image-1", "medium", "auto"): 0.042,
    ("gpt-image-1", "high", "1024x1024"): 0.167,
    ("gpt-image-1", "high", "1024x1536"): 0.250,
    ("gpt-image-1", "high", "1536x1024"): 0.250,
    ("gpt-image-1", "high", "auto"): 0.167,
}


class DalleProvider(SyncProvider):
    """Provider adapter for OpenAI image generation (DALL-E 3, gpt-image-1).

    Models: ``gpt-image-1`` (latest), ``dall-e-3``, ``dall-e-2``.

    .. warning::
        DALL-E 2/3 return temporary CDN URLs that expire (~1 hour).
        Use ``ObjectStorageSink`` to upload assets immediately, or use
        ``gpt-image-1`` which saves images locally as base64.

    Args:
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        http_timeout: HTTP request timeout in seconds (default 60).
        output_dir: Directory for saved images (default system temp).
            Required for gpt-image-1 which returns base64 data.
    """

    name = "openai-dalle"

    def get_capabilities(self) -> ProviderCapabilities:
        """DALL-E / gpt-image-1: image generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            models=["gpt-image-1", "dall-e-3", "dall-e-2"],
            output_formats=["image/png"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        http_timeout: float = 60.0,
        output_dir: str | Path | None = None,
    ):
        super().__init__()
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

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

    def _validate_params(self, step: Step) -> None:
        """Validate size and quality against model-specific constraints."""
        valid_sizes = _SIZE_BY_MODEL.get(step.model)
        if valid_sizes and "size" in step.params:
            if step.params["size"] not in valid_sizes:
                raise ProviderError(
                    f"Invalid size={step.params['size']!r} for {step.model}. "
                    f"Must be one of {valid_sizes}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
        if "quality" in step.params and step.params["quality"] not in _VALID_QUALITIES:
            raise ProviderError(
                f"Invalid quality={step.params['quality']!r}. Must be one of {_VALID_QUALITIES}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )

    def _save_b64_image(self, b64_data: str, step: Step, index: int) -> str:
        """Decode base64 image data and save to file. Returns file:// URI."""
        img_bytes = base64.b64decode(b64_data)
        suffix = ".png"
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}_{index}{suffix}"
            out_path.write_bytes(img_bytes)
        else:
            fd, tmp = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            out_path = Path(tmp)
            out_path.write_bytes(img_bytes)
        return f"file://{quote(str(out_path.resolve()))}"

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate image(s) via the OpenAI Images API."""
        client = self._get_client()
        self._validate_params(step)
        try:
            is_b64 = step.model in _B64_ONLY_MODELS
            params: dict = {
                "model": step.model,
                "prompt": step.prompt or "",
                "response_format": "b64_json" if is_b64 else "url",
            }

            if "size" in step.params:
                params["size"] = step.params["size"]
            if "quality" in step.params:
                params["quality"] = step.params["quality"]
            if "n" in step.params:
                params["n"] = int(step.params["n"])
            if "style" in step.params:
                params["style"] = step.params["style"]
            if "background" in step.params:
                params["background"] = step.params["background"]

            response = client.images.generate(**params)

            for i, img in enumerate(response.data):
                if is_b64:
                    # gpt-image-1: decode base64 data and save locally
                    b64 = img.b64_json
                    if not b64:
                        continue
                    file_url = self._save_b64_image(b64, step, i)
                    step.assets.append(Asset(url=file_url, media_type="image/png"))
                else:
                    # DALL-E 2/3: use URL directly (expires ~1 hour)
                    url = img.url
                    if url:
                        validate_asset_url(url)
                        step.assets.append(Asset(url=url, media_type="image/png"))

            if not is_b64 and step.assets:
                logger.warning(
                    "%s returns temporary URLs that expire (~1 hour). "
                    "Use ObjectStorageSink to persist assets, or switch to gpt-image-1.",
                    step.model,
                )

            n_images = len(step.assets)
            quality = step.params.get("quality", "standard")
            size = step.params.get("size", "1024x1024")
            per_image = _PRICING.get((step.model, quality, size))
            if per_image is not None:
                step.cost_usd = per_image * n_images

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"DALL-E generation failed: {exc}",
                error_code=map_openai_error(exc),
            ) from exc
