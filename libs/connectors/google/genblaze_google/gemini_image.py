"""GeminiImageProvider — adapter for Gemini-native image generation.

Synchronous API: client.models.generate_content() returns inline base64
image bytes in ``candidates[].content.parts[].inline_data``, NOT the
``:predict`` shape ``ImagenProvider`` uses. Different wire shape, not a
pattern widening of Imagen — see issue #205.

**Why this provider exists:** on a freshly created Gemini API key, every
``imagen-*`` slug 404s "no longer available to new users" (an account
entitlement gate — see ``google_imagen_predict_probe`` / issue #206). The
Gemini-native ``*-image`` line (``gemini-2.5-flash-image``,
``gemini-3.1-flash-image``, etc.) speaks ``generateContent`` instead of
``:predict`` and has no reported entitlement gap, making it the only
image path a new key can actually call.

**Catalog architecture:** ships the pattern-keyed ``google-gemini-image``
family (``^gemini-.*-image``, deliberately excludes chat models like
``gemini-2.5-flash``). No known entitlement gap for this family, so it
uses the plain ``google_models_get_probe`` (unlike Imagen).

Docs: https://ai.google.dev/gemini-api/docs/image-generation
"""

from __future__ import annotations

import mimetypes
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
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    SyncProvider,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_google._client import GoogleClientMixin
from genblaze_google._errors import map_google_error
from genblaze_google._families import GOOGLE_GEMINI_IMAGE_FAMILY

_FALLBACK = ModelSpec(model_id="*", modality=Modality.IMAGE)

# finish_reason values that indicate a safety refusal rather than a
# generic "no image" outcome — distinguishes CONTENT_POLICY (deterministic,
# never retryable) from a plain INVALID_INPUT when a candidate carries no
# inline image data.
_SAFETY_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "PROHIBITED_CONTENT",
        "SPII",
        "BLOCKLIST",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
    }
)

_DEFAULT_IMAGE_SUFFIX = ".png"


def _finish_reason_name(candidate: Any) -> str | None:
    """Normalize the SDK's FinishReason enum (or plain str) to its bare name."""
    reason = getattr(candidate, "finish_reason", None)
    if reason is None:
        return None
    return str(reason).rsplit(".", 1)[-1]


class GeminiImageProvider(GoogleClientMixin, SyncProvider):
    """Provider adapter for Gemini-native image generation.

    Models match the ``google-gemini-image`` family (``^gemini-.*-image``).
    Current examples: ``gemini-2.5-flash-image``, ``gemini-3.1-flash-image``.

    Unlike Imagen, Gemini image models return inline base64 bytes via
    ``generateContent`` rather than an Imagen-style prediction object.
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

    name = "google-gemini-image"
    discovery_support = DiscoverySupport.PARTIAL
    """google-genai exposes ``client.models.get`` per-slug; that's the
    authoritative liveness signal. There's no image-only catalog listing
    endpoint, so we stay PARTIAL and rely on the family probe."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(GOOGLE_GEMINI_IMAGE_FAMILY,),
            fallback=_FALLBACK,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """Gemini image: image generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            models=self._models.known(),
            output_formats=["image/png", "image/jpeg", "image/webp"],
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

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate image(s) via Gemini's generateContent, save inline bytes to disk."""
        client = self._get_client()
        try:
            # Registry pipeline for parity with Veo/Imagen — validates
            # step.inputs (SSRF) even though this family has no param
            # constraints of its own yet.
            payload = self.prepare_payload(step)
            prompt = payload.get("prompt", step.prompt or "")

            from google.genai import types as genai_types

            # Gemini image models only emit inline image parts when the request
            # explicitly asks for the IMAGE modality; without response_modalities
            # the API can return a text description and we'd wrongly fall into
            # the "no inline image data" branch on every real call (#205).
            response = client.models.generate_content(
                model=step.model,
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                config=genai_types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )

            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                # A blocked *prompt* returns zero candidates with the reason on
                # response.prompt_feedback (distinct from a candidate-level
                # finish_reason). Map a policy block to CONTENT_POLICY so it
                # isn't misreported as a generic, retryable INVALID_INPUT (#205).
                block = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
                if block is not None:
                    raise ProviderError(
                        f"Gemini blocked the image prompt "
                        f"(block_reason={str(block).rsplit('.', 1)[-1]})",
                        error_code=ProviderErrorCode.CONTENT_POLICY,
                    )
                raise ProviderError(
                    "Gemini returned no candidates for image generation",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )

            first = candidates[0]
            parts = getattr(getattr(first, "content", None), "parts", None) or []

            written = 0
            for part in parts:
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline is not None else None
                if not data:
                    continue
                mime_type = getattr(inline, "mime_type", None) or "image/png"
                suffix = mimetypes.guess_extension(mime_type) or _DEFAULT_IMAGE_SUFFIX

                if self._output_dir:
                    self._output_dir.mkdir(parents=True, exist_ok=True)
                    out_path = self._output_dir / f"{step.step_id}_{written}{suffix}"
                else:
                    fd, tmp = tempfile.mkstemp(suffix=suffix)
                    os.close(fd)
                    out_path = Path(tmp)

                out_path.write_bytes(data)
                file_url = local_file_url(out_path.resolve())
                step.assets.append(Asset(url=file_url, media_type=mime_type))
                written += 1

            if written == 0:
                # No inline image parts — distinguish a safety refusal
                # (deterministic, never retryable) from a generic empty
                # response so callers get an actionable error code.
                reason = _finish_reason_name(first)
                if reason in _SAFETY_FINISH_REASONS:
                    raise ProviderError(
                        f"Gemini image generation refused on safety grounds "
                        f"(finish_reason={reason})",
                        error_code=ProviderErrorCode.CONTENT_POLICY,
                    )
                raise ProviderError(
                    f"Gemini returned no inline image data (finish_reason={reason})",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Gemini image generation failed: {exc}",
                error_code=map_google_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
