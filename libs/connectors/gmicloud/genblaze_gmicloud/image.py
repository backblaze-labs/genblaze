"""GMICloudImageProvider — image generation via the GMICloud request queue.

Auth: Set GMI_API_KEY env var or pass api_key= to the constructor.

Docs: https://docs.gmicloud.ai
"""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    ProviderCapabilities,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from ._base import GMICloudBase
from ._errors import map_gmicloud_error

# Per-generation pricing by model (USD) — approximate, based on GMICloud tiers.
# Unknown models pass through to the API; cost_usd will be None.
_IMAGE_PRICING: dict[str, float] = {
    "Seedream-5.0-Lite": 0.035,
    "Gemini-2.5-Flash-Image": 0.039,
    "Reve-Edit-Fast": 0.007,
    "FLUX-Kontext-Pro": 0.05,
    "Seededit": 0.03,
    "Bria-Blending": 0.02,
    "Bria-Relighting": 0.02,
    "Bria-Restoration": 0.02,
}


class GMICloudImageProvider(GMICloudBase):
    """Provider adapter for GMICloud image generation via the request queue.

    Models: Seedream, Gemini Flash Image, FLUX-Kontext, Reve, Bria series,
    and any new image model added to GMICloud's queue (unknown models pass through).

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
    """

    name = "gmicloud-image"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=sorted(_IMAGE_PRICING),
            output_formats=["image/png", "image/jpeg"],
        )

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        try:
            payload: dict = {}
            if step.prompt:
                payload["prompt"] = step.prompt

            for key in ("aspect_ratio", "negative_prompt", "number_of_images"):
                if key in step.params:
                    payload[key] = step.params[key]

            if step.seed is not None:
                payload["seed"] = step.seed

            if step.inputs and len(step.inputs) > 0:
                validate_chain_input_url(step.inputs[0].url)
                payload["image"] = step.inputs[0].url

            return self._submit_request(step.model, payload)

        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud submit failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        try:
            detail = self._fetch_detail(prediction_id)
            status = detail.get("status", "")
            outcome = detail.get("outcome") or {}

            step.provider_payload = {"gmicloud": {"request_id": prediction_id, "status": status}}

            if status in ("failed", "cancelled"):
                raise ProviderError(
                    str(detail.get("error") or f"Image generation {status}"),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            image_url = outcome.get("image_url") or outcome.get("url")
            if not image_url:
                raise ProviderError("GMICloud request completed but no image URL found")

            validate_asset_url(str(image_url))

            path = urlparse(str(image_url)).path
            mime, _ = mimetypes.guess_type(path)
            if mime is None or not mime.startswith("image/"):
                mime = "image/png"

            step.assets.append(Asset(url=str(image_url), media_type=mime))

            per_gen = _IMAGE_PRICING.get(step.model)
            if per_gen is not None:
                step.cost_usd = per_gen * len(step.assets)

            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud fetch_output failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc
