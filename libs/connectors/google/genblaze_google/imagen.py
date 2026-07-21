"""ImagenProvider — adapter for Google Imagen image generation.

Synchronous API: client.models.generate_images() returns images directly.

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships the
pattern-keyed ``google-imagen`` family (``^imagen-``) instead of a
hardcoded slug list. Authoritative liveness comes from
``client.models.get(model=slug)`` via the family probe, so dead /
unauthorized slugs surface at preflight rather than mid-call.

**Pricing**: per-image-by-model rates were dropped in 0.3.0. See
``docs/reference/pricing-recipes.md`` for the canonical Imagen
recipe.

Docs: https://ai.google.dev/gemini-api/docs/image-generation
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoverySupport,
    LiveProbeResult,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_google._errors import map_google_error
from genblaze_google._families import GOOGLE_IMAGEN_FAMILY

_FALLBACK = ModelSpec(model_id="*", modality=Modality.IMAGE)


class ImagenProvider(SyncProvider):
    """Provider adapter for Google Imagen image generation.

    Models match the ``google-imagen`` family (``^imagen-``). Current
    examples: ``imagen-3.0-generate-002``, ``imagen-3.0-fast-generate-001``.

    Imagen returns image bytes directly (synchronous, not operation-based).
    Output is saved to files; use ObjectStorageSink for cloud upload.

    Args:
        api_key: Gemini API key. Falls back to GEMINI_API_KEY env var.
        project: GCP project ID for Vertex AI auth.
        location: GCP region for Vertex AI (default "us-central1").
        output_dir: Directory for output image files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL.
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "google-imagen"
    discovery_support = DiscoverySupport.PARTIAL
    """google-genai exposes ``client.models.get`` per-slug; that's the
    authoritative liveness signal. There's no Imagen-only catalog
    listing endpoint, so we stay PARTIAL and rely on the family
    probe."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(GOOGLE_IMAGEN_FAMILY,),
            fallback=_FALLBACK,
        )

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
        self._project = project
        self._location = location
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def _invoke_family_probe(self, probe: Any, model_id: str) -> LiveProbeResult:
        """Forward the family probe with this provider's lazy genai client."""
        return probe(model_id, client=self._get_client())

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
                file_url = local_file_url(out_path.resolve())
                step.assets.append(Asset(url=file_url, media_type=mime_type))

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Imagen generation failed: {exc}",
                error_code=map_google_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
