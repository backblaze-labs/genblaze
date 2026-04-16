"""ImagenProvider — adapter for Google Imagen image generation.

Synchronous API: client.models.generate_images() returns images directly.

Docs: https://ai.google.dev/gemini-api/docs/image-generation
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

from genblaze_google._errors import map_google_error

_VALID_ASPECT_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9"}

# Per-image pricing by model (USD)
_IMAGEN_PRICING: dict[str, float] = {
    "imagen-3.0-generate-002": 0.04,
    "imagen-3.0-fast-generate-001": 0.02,
}


class ImagenProvider(SyncProvider):
    """Provider adapter for Google Imagen image generation.

    Models: ``imagen-3.0-generate-002`` (latest), ``imagen-3.0-fast-generate-001``.

    Imagen returns image bytes directly (synchronous, not operation-based).
    Output is saved to files; use ObjectStorageSink for cloud upload.

    Args:
        api_key: Gemini API key. Falls back to GEMINI_API_KEY env var.
        project: GCP project ID for Vertex AI auth.
        location: GCP region for Vertex AI (default "us-central1").
        output_dir: Directory for output image files (default system temp).
    """

    name = "google-imagen"

    def get_capabilities(self) -> ProviderCapabilities:
        """Imagen: image generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            models=["imagen-3.0-generate-002", "imagen-3.0-fast-generate-001"],
            output_formats=["image/png", "image/jpeg"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project: str | None = None,
        location: str = "us-central1",
        output_dir: str | Path | None = None,
    ):
        super().__init__()
        self._api_key = api_key
        self._project = project
        self._location = location
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ProviderError(
                    "google-genai package not installed. Run: pip install google-genai"
                ) from exc
            if self._project:
                self._client = genai.Client(
                    vertexai=True, project=self._project, location=self._location
                )
            else:
                kwargs: dict = {}
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                self._client = genai.Client(**kwargs)
        return self._client

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate image(s) via the Google Imagen API."""
        client = self._get_client()
        try:
            from google.genai import types

            config_kwargs: dict = {}

            if "number_of_images" in step.params:
                config_kwargs["number_of_images"] = int(step.params["number_of_images"])
            if "aspect_ratio" in step.params:
                ar = step.params["aspect_ratio"]
                if ar not in _VALID_ASPECT_RATIOS:
                    raise ProviderError(
                        f"Invalid aspect_ratio={ar!r}. Must be one of {_VALID_ASPECT_RATIOS}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                config_kwargs["aspect_ratio"] = ar
            if "person_generation" in step.params:
                config_kwargs["person_generation"] = step.params["person_generation"]
            if "safety_filter_level" in step.params:
                config_kwargs["safety_filter_level"] = step.params["safety_filter_level"]
            if "output_mime_type" in step.params:
                config_kwargs["output_mime_type"] = step.params["output_mime_type"]

            gen_config = types.GenerateImagesConfig(**config_kwargs) if config_kwargs else None

            kwargs: dict = {"model": step.model, "prompt": step.prompt or ""}
            if gen_config is not None:
                kwargs["config"] = gen_config

            response = client.models.generate_images(**kwargs)

            # Safety filter can return 0 images without raising an error
            if not response.generated_images:
                raise ProviderError(
                    "Imagen returned no images — prompt may have been blocked by safety filters",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )

            mime_type = step.params.get("output_mime_type", "image/png")
            suffix = ".jpeg" if "jpeg" in mime_type else ".png"

            for i, img in enumerate(response.generated_images):
                if self._output_dir:
                    self._output_dir.mkdir(parents=True, exist_ok=True)
                    out_path = self._output_dir / f"{step.step_id}_{i}{suffix}"
                else:
                    fd, tmp = tempfile.mkstemp(suffix=suffix)
                    os.close(fd)
                    out_path = Path(tmp)

                img.image.save(str(out_path))
                file_url = f"file://{quote(str(out_path.resolve()))}"
                step.assets.append(Asset(url=file_url, media_type=mime_type))

            # Track cost
            price = _IMAGEN_PRICING.get(step.model)
            if price is not None:
                step.cost_usd = price * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Imagen generation failed: {exc}",
                error_code=map_google_error(exc),
            ) from exc
