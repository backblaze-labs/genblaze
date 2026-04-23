"""ImagenProvider — adapter for Google Imagen image generation.

Synchronous API: client.models.generate_images() returns images directly.

Models, pricing, and parameter handling are driven by the ``ModelRegistry``
returned from ``create_registry()``. Users can override pricing or register
new models via::

    provider = ImagenProvider(models=my_registry)

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
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    SyncProvider,
    per_unit,
)
from genblaze_core.runnable.config import RunnableConfig

from genblaze_google._errors import map_google_error

_VALID_ASPECT_RATIOS = frozenset({"1:1", "3:4", "4:3", "9:16", "16:9"})

# Per-image pricing by model (USD). Captured here once; the specs wrap these
# in ``per_unit`` pricing strategies.
_IMAGEN_PER_IMAGE_RATES: dict[str, float] = {
    "imagen-3.0-generate-002": 0.04,
    "imagen-3.0-fast-generate-001": 0.02,
}


def _check_aspect_ratio(params: dict[str, Any]) -> None:
    """Validate aspect_ratio with Imagen-specific error wording."""
    ar = params.get("aspect_ratio")
    if ar is not None and ar not in _VALID_ASPECT_RATIOS:
        raise ProviderError(
            f"Invalid aspect_ratio={ar!r}. Must be one of {_VALID_ASPECT_RATIOS}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _imagen_spec(model_id: str) -> ModelSpec:
    """Per-model spec — flat per-image pricing, aspect_ratio validation."""
    rate = _IMAGEN_PER_IMAGE_RATES.get(model_id)
    return ModelSpec(
        model_id=model_id,
        modality=Modality.IMAGE,
        pricing=per_unit(rate) if rate is not None else None,
        param_constraints=(_check_aspect_ratio,),
    )


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
        models: Optional custom ``ModelRegistry`` — overrides the class default.
    """

    name = "google-imagen"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        defaults = {mid: _imagen_spec(mid) for mid in _IMAGEN_PER_IMAGE_RATES}
        return ModelRegistry(defaults=defaults)

    def get_capabilities(self) -> ProviderCapabilities:
        """Imagen: image generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["image/png", "image/jpeg"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project: str | None = None,
        location: str = "us-central1",
        output_dir: str | Path | None = None,
        models: ModelRegistry | None = None,
    ):
        super().__init__(models=models)
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

            # Run the registry pipeline — enforces aspect_ratio constraint with
            # the legacy error wording before we touch the SDK.
            payload = self.prepare_payload(step)

            config_kwargs: dict = {}
            if "number_of_images" in payload:
                config_kwargs["number_of_images"] = int(payload["number_of_images"])
            if "aspect_ratio" in payload:
                config_kwargs["aspect_ratio"] = payload["aspect_ratio"]
            if "person_generation" in payload:
                config_kwargs["person_generation"] = payload["person_generation"]
            if "safety_filter_level" in payload:
                config_kwargs["safety_filter_level"] = payload["safety_filter_level"]
            if "output_mime_type" in payload:
                config_kwargs["output_mime_type"] = payload["output_mime_type"]

            gen_config = types.GenerateImagesConfig(**config_kwargs) if config_kwargs else None

            kwargs: dict = {
                "model": step.model,
                "prompt": payload.get("prompt", step.prompt or ""),
            }
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

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Imagen generation failed: {exc}",
                error_code=map_google_error(exc),
            ) from exc
