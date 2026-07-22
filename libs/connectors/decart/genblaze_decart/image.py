"""DecartImageProvider — adapter for Decart Lucy image generation.

Uses the synchronous process() API for image models.

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships a
pattern-keyed ``ModelFamily`` rather than a hardcoded slug list. The
``decart-lucy-image`` family captures any ``lucy-`` slug ending in
``2i`` (t2i, i2i — current and future Lucy image variants).

**DiscoverySupport.NONE**: same rationale as the video provider — no
``GET /v1/models`` endpoint, decart SDK doesn't expose raw HTTP, small
stable catalog.

**Pricing**: previously hardcoded at ``$0.02 / generation``. As of
0.3.0 the SDK no longer ships pricing — see
``docs/reference/pricing-recipes.md`` for the canonical Decart recipe.

Docs: https://docs.platform.decart.ai/
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from genblaze_core._utils import _run_async, local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoverySupport,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_decart_error

# Lucy image family — pattern matches t2i / i2i variants. Future Lucy
# image slugs (lucy-3-t2i, lucy-edit-i2i, etc.) inherit automatically.
_DECART_LUCY_IMAGE_FAMILY = ModelFamily(
    name="decart-lucy-image",
    pattern=re.compile(r"^lucy-.*2i$"),
    spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
    description="Decart Lucy image family — text-to-image and image-to-image variants.",
    example_slugs=("lucy-pro-t2i", "lucy-pro-i2i"),
)


_FALLBACK = ModelSpec(model_id="*", modality=Modality.IMAGE)


class DecartImageProvider(SyncProvider):
    """Provider adapter for Decart Lucy image generation.

    Models match the ``decart-lucy-image`` family — any ``lucy-*-2i`` slug.

    Auth: Set DECART_API_KEY env var or pass api_key.

    Args:
        api_key: Decart API key. Falls back to DECART_API_KEY env var.
        output_dir: Directory for output files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL (no-op for NONE).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "decart-image"
    discovery_support = DiscoverySupport.NONE
    """Same rationale as DecartVideoProvider — no /v1/models endpoint,
    SDK-only access, small stable catalog."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_DECART_LUCY_IMAGE_FAMILY,),
            fallback=_FALLBACK,
        )

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
            file_url = local_file_url(out_path.resolve())
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
