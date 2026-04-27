"""DecartImageProvider — adapter for Decart Lucy image generation.

Uses the synchronous process() API for image models.

Models and pricing are driven by the ``ModelRegistry`` returned from
``create_registry()``. Image pricing is a flat per-generation rate.

Docs: https://docs.platform.decart.ai/
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core._utils import _run_async
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
    per_unit,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_decart_error

# Supported image models
_IMAGE_MODELS = (
    "lucy-pro-t2i",
    "lucy-pro-i2i",
)

# Flat per-generation price (USD) across all Decart image models.
_IMAGE_PRICE = 0.02


def _image_spec(model_id: str) -> ModelSpec:
    """Per-model spec — flat per-asset pricing."""
    return ModelSpec(
        model_id=model_id,
        modality=Modality.IMAGE,
        pricing=per_unit(_IMAGE_PRICE),
    )


class DecartImageProvider(SyncProvider):
    """Provider adapter for Decart Lucy image generation.

    Models: ``lucy-pro-t2i``, ``lucy-pro-i2i``.

    Auth: Set DECART_API_KEY env var or pass api_key.

    Args:
        api_key: Decart API key. Falls back to DECART_API_KEY env var.
        output_dir: Directory for output files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "decart-image"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(defaults={mid: _image_spec(mid) for mid in _IMAGE_MODELS})

    def get_capabilities(self) -> ProviderCapabilities:
        """Decart Lucy: image generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            output_formats=["image/png"],
            models=sorted(self._models.known()),
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
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                from decart import DecartClient
            except ImportError as exc:
                raise ProviderError(
                    "decart package not installed. Run: pip install decart"
                ) from exc
            kwargs: dict = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = DecartClient(**kwargs)
        return self._client

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate an image via the Decart synchronous process API."""
        client = self._get_client()
        try:
            from decart import models

            params: dict = {
                "model": models.image(step.model),  # type: ignore[arg-type]
                "prompt": step.prompt or "",
            }

            result = _run_async(client.process(params))

            # Save image to file
            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}.png"
            else:
                fd, tmp = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                out_path = Path(tmp)

            out_path.write_bytes(result.data)
            file_url = f"file://{quote(str(out_path.resolve()))}"
            step.assets.append(Asset(url=file_url, media_type="image/png"))

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Decart image generation failed: {exc}",
                error_code=map_decart_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
