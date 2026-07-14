"""SoraProvider — adapter for the OpenAI Videos API (Sora).

Uses the asynchronous job-based Videos API:
  POST /v1/videos → poll GET /v1/videos/{id} → download content

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships a
pattern-keyed ``ModelFamily`` plus ``DiscoverySupport.NATIVE`` discovery
via ``client.models.list()`` (filtered to ``^sora-`` slugs).

**Pricing**: still ``None`` — the correct formula requires
``(model, size, seconds)`` per-second billing and a flat per-video dict
would misreport cost by 10x+ on longer clips. Re-enable via
``register_pricing()`` if a sound formula is available; see
``docs/reference/pricing-recipes.md`` for the canonical guidance.

Docs: https://platform.openai.com/docs/api-reference/videos
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoveryResult,
    DiscoverySupport,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    route_images,
)
from genblaze_core.providers.base import BaseProvider
from genblaze_core.providers.discovery import DEFAULT_TTL_SECONDS, _DiscoveryCache
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_openai._errors import map_openai_error

# Reuse DalleProvider's file-input machinery (SSRF-pinned https download +
# allowlisted file:// resolution) rather than duplicating it here — Sora's
# image-to-video input has the exact same shape (chain output as a local
# file:// temp path, or a user-supplied https:// URL).
from genblaze_openai.dalle import _download_https_to_temp, _resolve_local_file

logger = logging.getLogger("genblaze.openai.sora")

# Valid Sora sizes (width x height)
_VALID_SIZES = frozenset({"720x1280", "1280x720", "1024x1792", "1792x1024"})
_VALID_SECONDS = frozenset({4, 8, 12})

# Map standard resolution + aspect_ratio to Sora's size format.
# This is the canonical many-to-one param transformer: (resolution, aspect_ratio)
# collapse to a single `size` string.
_RESOLUTION_TO_SIZE: dict[tuple[str, str], str] = {
    ("1080p", "16:9"): "1280x720",
    ("720p", "16:9"): "1280x720",
    ("1080p", "9:16"): "720x1280",
    ("720p", "9:16"): "720x1280",
}


def _sora_param_transformer(params: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``(resolution, aspect_ratio) → size`` and ``duration → seconds``.

    Sora caps landscape at 720p — a 1080p request downgrades to 720p with a
    warning (matches the historical ``normalize_params`` behavior).
    """
    out = dict(params)
    # duration → seconds
    if "duration" in out and "seconds" not in out:
        out["seconds"] = out.pop("duration")
    # resolution + aspect_ratio → size
    if "resolution" in out and "size" not in out:
        ar = out.get("aspect_ratio", "16:9")
        requested = out["resolution"]
        key = (requested, ar)
        if key in _RESOLUTION_TO_SIZE:
            mapped = _RESOLUTION_TO_SIZE[key]
            if requested == "1080p" and mapped == "1280x720":
                logger.warning(
                    "Sora does not support 1080p for %s — downgrading to 720p (1280x720)",
                    ar,
                )
            out["size"] = mapped
        out.pop("resolution", None)
        out.pop("aspect_ratio", None)
    return out


def _validate_seconds(params: dict[str, Any]) -> None:
    """Preserve the bespoke ``Invalid seconds=...`` wording the tests assert."""
    if "seconds" not in params:
        return
    try:
        seconds = int(params["seconds"])
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            f"Invalid seconds={params['seconds']!r}. Must be one of {set(_VALID_SECONDS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        ) from exc
    if seconds not in _VALID_SECONDS:
        raise ProviderError(
            f"Invalid seconds={seconds}. Must be one of {set(_VALID_SECONDS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    params["seconds"] = seconds


def _validate_size(params: dict[str, Any]) -> None:
    """Preserve the bespoke ``Invalid size=...`` wording the tests assert."""
    if "size" not in params:
        return
    size = params["size"]
    if size not in _VALID_SIZES:
        raise ProviderError(
            f"Invalid size={size}. Must be one of {set(_VALID_SIZES)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


# Single Sora family — covers sora-2, sora-2-pro, future variants.
# Constraints (size enum, seconds enum) and the param transformer
# (resolution+aspect_ratio → size, duration → seconds) ride on the
# spec_template so every Sora slug inherits them automatically.
_OPENAI_SORA_FAMILY = ModelFamily(
    name="openai-sora",
    pattern=re.compile(r"^sora-"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_transformer=_sora_param_transformer,
        param_constraints=(_validate_seconds, _validate_size),
        # Route the first image asset to the native `image` slot for image-to-video.
        input_mapping=route_images(slots=("image",)),
    ),
    description="OpenAI Sora video family — sora-2, sora-2-pro, future variants.",
    example_slugs=("sora-2", "sora-2-pro"),
)


_FALLBACK = ModelSpec(model_id="*", modality=Modality.VIDEO)


class SoraProvider(BaseProvider):
    """Provider adapter for OpenAI Sora video generation.

    Models match the ``openai-sora`` family — any ``^sora-`` slug.

    Args:
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        poll_interval: Seconds between status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 60).
        output_dir: Directory for output files (default system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL (no-op for NATIVE
            but accepted for API uniformity).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "openai-sora"
    discovery_support = DiscoverySupport.NATIVE
    """OpenAI exposes ``client.models.list()`` as the authoritative
    catalog endpoint. The fetcher filters to family-matched slugs so
    chat / image / TTS slugs don't pollute the Sora cache."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_OPENAI_SORA_FAMILY,),
            fallback=_FALLBACK,
        )

    def __init__(
        self,
        api_key: str | None = None,
        poll_interval: float = 5.0,
        http_timeout: float = 60.0,
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
        self.poll_interval = poll_interval
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None
        # Wire NATIVE discovery — fetcher closes over self so it picks
        # up the lazy-initialized openai client.
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_models,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """Sora: video generation from text prompts."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    # --- catalog discovery (DiscoverySupport.NATIVE) ----------------------

    def _fetch_models(self) -> DiscoveryResult:
        """Fetcher backing ``discover_models`` — calls /v1/models, filters to Sora.

        OpenAI's ``models.list()`` returns the global catalog (chat,
        embeddings, images, audio, video). We filter to slugs that
        match the Sora family pattern so a user passing e.g. ``gpt-4o``
        to Sora gets ``NOT_FOUND`` at preflight rather than a misleading
        ``OK_AUTHORITATIVE`` from the cross-modality catalog.
        """
        try:
            client = self._get_client()
            response = client.models.list()
            slugs: set[str] = set()
            for model in response.data:
                mid = getattr(model, "id", None)
                if isinstance(mid, str) and self._models.match_family(mid) is not None:
                    slugs.add(mid)
            return DiscoveryResult.ok(slugs, source_url="https://api.openai.com/v1/models")
        except Exception as exc:
            return DiscoveryResult.failed(
                f"OpenAI models.list() failed: {exc}",
                source_url="https://api.openai.com/v1/models",
            )

    def discover_models(
        self,
        *,
        max_age_seconds: float | None = ...,  # type: ignore[assignment]
    ) -> DiscoveryResult:
        """Snapshot the Sora-filtered OpenAI catalog. Single-flight, TTL-bounded."""
        cache = self._models._discovery_cache
        assert cache is not None  # wired in __init__
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            return cache.get()
        return cache.get(max_age_seconds=max_age_seconds)

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

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create a video generation job via POST /v1/videos."""
        client = self._get_client()
        open_file: Any = None
        temp_to_clean: Path | None = None
        try:
            # Registry pipeline validates seconds/size, applies resolution+aspect
            # transformer, routes first image input, and SSRF-validates inputs.
            payload = self.prepare_payload(step)

            params: dict = {
                "model": step.model,
                "prompt": payload.get("prompt", step.prompt or ""),
            }
            for key in ("seconds", "size"):
                if key in payload:
                    params[key] = payload[key]
            if "seconds" in params:
                # openai SDK's VideoSeconds is Literal["4", "8", "12"] — not int.
                params["seconds"] = str(params["seconds"])

            # route_images(slots=("image",)) hands back a plain asset URL, but
            # Videos.create() has no `image` kwarg — the start frame goes in
            # `input_reference` as an uploaded file. Chain inputs arrive as
            # local file:// temp paths (sink upload happens later); direct
            # inputs may be https:// URLs. Both are materialized to an open
            # file handle before upload (see #126).
            image_url = payload.get("image")
            if image_url is not None:
                parsed = urlparse(image_url)
                if parsed.scheme == "file":
                    local_path = _resolve_local_file(image_url, self._output_dir)
                else:
                    local_path = _download_https_to_temp(image_url, self._http_timeout)
                    temp_to_clean = local_path
                open_file = local_path.open("rb")
                params["input_reference"] = open_file

            response = client.videos.create(**params)
            return response.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Sora submit failed: {exc}",
                error_code=map_openai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
        finally:
            if open_file is not None:
                with contextlib.suppress(Exception):
                    open_file.close()
            if temp_to_clean is not None:
                with contextlib.suppress(Exception):
                    temp_to_clean.unlink(missing_ok=True)

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check video generation status via GET /v1/videos/{id}."""
        client = self._get_client()
        try:
            video = client.videos.retrieve(prediction_id)
            if video.status in ("completed", "failed"):
                self._cache_poll_result(prediction_id, video)
                return True
            return False
        except Exception as exc:
            raise ProviderError(
                f"Sora poll failed: {exc}",
                error_code=map_openai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch completed video, download with auth, and save locally."""
        client = self._get_client()
        try:
            video = self._get_cached_poll_result(prediction_id)
            if video is None:
                video = client.videos.retrieve(prediction_id)

            step.provider_payload = {
                "openai": {
                    "video_id": video.id,
                    "model": video.model if hasattr(video, "model") else None,
                    "status": video.status,
                }
            }

            if video.status == "failed":
                error_msg = getattr(video, "error", None) or "Video generation failed"
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Content endpoint requires the API key in the Authorization header.
            # openai SDK 2.x renamed videos.content → videos.download_content (#127).
            content = client.videos.download_content(prediction_id, variant="video")
            if self._output_dir:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                out_path = self._output_dir / f"{step.step_id}.mp4"
                content.write_to_file(str(out_path))
            else:
                fd, tmp = tempfile.mkstemp(suffix=".mp4")
                os.close(fd)
                out_path = Path(tmp)
                content.write_to_file(str(out_path))

            file_url = f"file://{quote(str(out_path.resolve()))}"
            asset = Asset(url=file_url, media_type="video/mp4")
            asset.video = VideoMetadata(has_audio=False, codec="h264")
            step.assets.append(asset)

            # Pricing intentionally disabled on the spec — see _sora_spec().
            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Sora fetch_output failed: {exc}",
                error_code=map_openai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
