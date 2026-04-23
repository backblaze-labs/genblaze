"""OpenAI image provider — adapter for /v1/images/generations and /v1/images/edits.

Supports OpenAI's full image model lineup: ``gpt-image-2``, ``gpt-image-1.5``,
``gpt-image-1``, ``gpt-image-1-mini``, ``dall-e-3``, ``dall-e-2``. Unknown /
alias models (e.g. ``chatgpt-image-latest``, dated snapshots) pass through
with ``cost_usd=None`` per the GMICloud "unknown models pass through"
convention.

Routing is driven by ``step.inputs`` presence: inputs → ``/images/edits``,
no inputs → ``/images/generations``. OpenAI is the authority for
model/endpoint compatibility — no client-side capability gating.

The class is still named ``DalleProvider`` and the provider identifier
stays ``openai-dalle`` for backward compatibility.

Docs:
- https://developers.openai.com/api/docs/guides/image-generation
- https://developers.openai.com/api/reference/python/resources/images/methods/edit
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import tempfile
import urllib.request
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urlparse

from genblaze_core._utils import check_ssrf
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    SyncProvider,
    tiered,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.runnable.config import RunnableConfig

from genblaze_openai._errors import map_openai_error

logger = logging.getLogger("genblaze.openai.dalle")

# --- Quality / size constants ------------------------------------------------

_GPT_IMAGE_FIXED_SIZES = frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"})
_DALLE3_SIZES = frozenset({"1024x1024", "1792x1024", "1024x1792"})
_DALLE2_SIZES = frozenset({"256x256", "512x512", "1024x1024"})

_GPT_QUALITIES = frozenset({"low", "medium", "high", "auto"})
_DALLE3_QUALITIES = frozenset({"standard", "hd"})
_DALLE2_QUALITIES = frozenset({"standard"})

_FORMAT_TO_MEDIA: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_FORMAT_TO_EXT: dict[str, str] = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}

# Max bytes for a single edit-input download (guards against malicious URLs)
_MAX_INPUT_BYTES = 50 * 1024 * 1024  # 50 MB — matches OpenAI's own edit limit

# --- Model registry ----------------------------------------------------------


@dataclass(frozen=True)
class _ImageModelSpec:
    """Per-model registry entry. Drives structural validation and cost.

    ``supports_input_fidelity`` is advisory — a mismatch emits a warning and
    still forwards the param. The server is the authority for capability
    rejection.
    """

    response_format: Literal["b64_json", "url"]
    valid_qualities: frozenset[str]
    fixed_sizes: frozenset[str] | None  # None => free-form (gpt-image-2)
    supports_input_fidelity: bool
    pricing: dict[tuple[str, str], float] | None


# Permissive fallback for unknown/alias models (chatgpt-image-latest, snapshots).
_DEFAULT_SPEC = _ImageModelSpec(
    response_format="b64_json",
    valid_qualities=_GPT_QUALITIES,
    fixed_sizes=None,
    supports_input_fidelity=True,
    pricing=None,
)


def _dalle_pricing(table: dict[tuple[str, str], float]):
    """Tiered pricing keyed by (quality, size) with sensible defaults."""

    def _key(ctx):
        params = ctx.step.params
        quality = params.get("quality")
        size = params.get("size", "1024x1024")
        if quality is None:
            # Default: prefer "auto" if present in the table, else "standard"
            quality = "auto" if ("auto", size) in table else "standard"
        return (quality, size)

    # Mapping is invariant in its key type; cast widens dict[tuple[str, str], ...]
    # to the tuple[Hashable, ...] shape `tiered` accepts.
    return tiered(cast(Mapping[tuple[Hashable, ...], float], table), key=_key)


_MODELS: dict[str, _ImageModelSpec] = {
    "gpt-image-2": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=None,  # free-form; see _validate_gpt_image_2_size
        supports_input_fidelity=False,  # native HF — param is no-op server-side
        pricing=None,  # TODO(pricing): set when OpenAI discloses per-image rates
    ),
    "gpt-image-1.5": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=_GPT_IMAGE_FIXED_SIZES,
        supports_input_fidelity=True,
        pricing={
            ("low", "1024x1024"): 0.009,
            ("low", "1024x1536"): 0.013,
            ("low", "1536x1024"): 0.013,
            ("low", "auto"): 0.009,
            ("medium", "1024x1024"): 0.034,
            ("medium", "1024x1536"): 0.050,
            ("medium", "1536x1024"): 0.050,
            ("medium", "auto"): 0.034,
            ("high", "1024x1024"): 0.133,
            ("high", "1024x1536"): 0.200,
            ("high", "1536x1024"): 0.200,
            ("high", "auto"): 0.133,
        },
    ),
    "gpt-image-1": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=_GPT_IMAGE_FIXED_SIZES,
        supports_input_fidelity=True,
        pricing={
            ("low", "1024x1024"): 0.011,
            ("low", "1024x1536"): 0.016,
            ("low", "1536x1024"): 0.016,
            ("low", "auto"): 0.011,
            ("medium", "1024x1024"): 0.042,
            ("medium", "1024x1536"): 0.063,
            ("medium", "1536x1024"): 0.063,
            ("medium", "auto"): 0.042,
            ("high", "1024x1024"): 0.167,
            ("high", "1024x1536"): 0.250,
            ("high", "1536x1024"): 0.250,
            ("high", "auto"): 0.167,
        },
    ),
    "gpt-image-1-mini": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=_GPT_IMAGE_FIXED_SIZES,
        supports_input_fidelity=False,  # per openai-python SDK reference
        pricing={
            ("low", "1024x1024"): 0.005,
            ("low", "1024x1536"): 0.006,
            ("low", "1536x1024"): 0.006,
            ("low", "auto"): 0.005,
            ("medium", "1024x1024"): 0.011,
            ("medium", "1024x1536"): 0.015,
            ("medium", "1536x1024"): 0.015,
            ("medium", "auto"): 0.011,
            ("high", "1024x1024"): 0.036,
            ("high", "1024x1536"): 0.052,
            ("high", "1536x1024"): 0.052,
            ("high", "auto"): 0.036,
        },
    ),
    "dall-e-3": _ImageModelSpec(
        response_format="url",
        valid_qualities=_DALLE3_QUALITIES,
        fixed_sizes=_DALLE3_SIZES,
        supports_input_fidelity=False,
        pricing={
            ("standard", "1024x1024"): 0.040,
            ("standard", "1024x1792"): 0.080,
            ("standard", "1792x1024"): 0.080,
            ("hd", "1024x1024"): 0.080,
            ("hd", "1024x1792"): 0.120,
            ("hd", "1792x1024"): 0.120,
        },
    ),
    "dall-e-2": _ImageModelSpec(
        response_format="url",
        valid_qualities=_DALLE2_QUALITIES,
        fixed_sizes=_DALLE2_SIZES,
        supports_input_fidelity=False,
        pricing={
            ("standard", "256x256"): 0.016,
            ("standard", "512x512"): 0.018,
            ("standard", "1024x1024"): 0.020,
        },
    ),
}


def _build_dalle_registry() -> ModelRegistry:
    """Expose models + pricing to the registry for user overrides.

    Parameter validation stays connector-side (free-form size rules for
    gpt-image-2, advisory input_fidelity warnings) — see ``_validate_params``.
    The registry surfaces:
    - Which models the connector knows (for capability discovery).
    - Per-model pricing as a ``tiered()`` strategy users can override.
    """
    defaults: dict[str, ModelSpec] = {}
    for model_id, spec in _MODELS.items():
        pricing = _dalle_pricing(spec.pricing) if spec.pricing else None
        defaults[model_id] = ModelSpec(model_id=model_id, pricing=pricing)
    return ModelRegistry(defaults=defaults)


# --- Validation --------------------------------------------------------------


def _parse_wxh(size: str) -> tuple[int, int]:
    """Parse 'WIDTHxHEIGHT' into (w, h) ints."""
    try:
        w_s, h_s = size.lower().split("x", 1)
        return int(w_s), int(h_s)
    except (ValueError, AttributeError) as exc:
        raise ProviderError(
            f"Invalid size {size!r}. Expected 'WIDTHxHEIGHT' or 'auto'.",
            error_code=ProviderErrorCode.INVALID_INPUT,
        ) from exc


def _validate_gpt_image_2_size(size: str) -> None:
    """Enforce gpt-image-2 free-form size constraints.

    Max edge < 3840 px, both edges multiples of 16, aspect ratio ≤ 3:1,
    total pixels 655,360 – 8,294,400.
    """
    if size == "auto":
        return
    w, h = _parse_wxh(size)
    if w <= 0 or h <= 0:
        raise ProviderError(
            f"Size {size!r}: width and height must be positive",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    if max(w, h) >= 3840:
        raise ProviderError(
            f"Size {size!r}: max edge must be < 3840px",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    if w % 16 != 0 or h % 16 != 0:
        raise ProviderError(
            f"Size {size!r}: both edges must be multiples of 16",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    ratio = max(w, h) / min(w, h)
    if ratio > 3.0:
        raise ProviderError(
            f"Size {size!r}: aspect ratio must be ≤ 3:1 (got {ratio:.2f}:1)",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    pixels = w * h
    if not 655_360 <= pixels <= 8_294_400:
        raise ProviderError(
            f"Size {size!r}: total pixels must be 655,360–8,294,400 (got {pixels:,})",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _validate_params(step: Step, spec: _ImageModelSpec) -> None:
    """Structural input validation. Capability flags are advisory (warn-only)."""
    params = step.params
    if "size" in params:
        size = str(params["size"])
        if spec.fixed_sizes is not None:
            if size not in spec.fixed_sizes:
                raise ProviderError(
                    f"Invalid size={size!r} for {step.model}. "
                    f"Must be one of {sorted(spec.fixed_sizes)}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
        elif step.model == "gpt-image-2":
            _validate_gpt_image_2_size(size)
    if "quality" in params:
        q = params["quality"]
        if q not in spec.valid_qualities:
            raise ProviderError(
                f"Invalid quality={q!r} for {step.model}. "
                f"Must be one of {sorted(spec.valid_qualities)}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
    if "output_compression" in params:
        c = params["output_compression"]
        if not isinstance(c, int) or not 0 <= c <= 100:
            raise ProviderError(
                f"Invalid output_compression={c!r}. Must be an integer 0–100.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
    if "input_fidelity" in params and not spec.supports_input_fidelity:
        logger.warning(
            "Model %s does not support input_fidelity per OpenAI docs; "
            "parameter forwarded but may be ignored or rejected by the server.",
            step.model,
        )


# --- Edit input resolution ---------------------------------------------------


_ALLOWED_FILE_ROOTS: tuple[Path, ...] = (Path(tempfile.gettempdir()).resolve(),)


def _resolve_local_file(url: str, extra_root: Path | None) -> Path:
    """Resolve a file:// URL to a Path, checked against allowed roots."""
    parsed = urlparse(url)
    resolved = Path(unquote(parsed.path)).resolve()
    allowed = list(_ALLOWED_FILE_ROOTS)
    if extra_root is not None:
        allowed.append(extra_root.resolve())
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise ProviderError(
            f"file:// URL outside allowed directories: {resolved}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    if not resolved.is_file():
        raise ProviderError(
            f"file:// URL does not exist: {resolved}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    return resolved


def _download_https_to_temp(url: str, timeout: float) -> Path:
    """Download an https:// URL to a temp file. SSRF-checked. Caller unlinks."""
    check_ssrf(url, exc_type=ProviderError)
    fd, tmp = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        req = urllib.request.Request(  # noqa: S310 (SSRF-checked above)
            url, headers={"User-Agent": "genblaze-openai"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (SSRF-checked above)
            size = 0
            with tmp_path.open("wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_INPUT_BYTES:
                        raise ProviderError(
                            f"Edit input exceeds {_MAX_INPUT_BYTES} byte limit",
                            error_code=ProviderErrorCode.INVALID_INPUT,
                        )
                    f.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


# --- Provider ----------------------------------------------------------------


class DalleProvider(SyncProvider):
    """Provider adapter for OpenAI image generation and editing.

    Models: ``gpt-image-2`` (newest), ``gpt-image-1.5``, ``gpt-image-1``,
    ``gpt-image-1-mini``, ``dall-e-3``, ``dall-e-2``. Unknown models
    (``chatgpt-image-latest``, dated snapshots) pass through with
    ``cost_usd=None``.

    The request routes to ``/images/edits`` when ``step.inputs`` is non-empty,
    otherwise ``/images/generations``. The server is the authority for
    model/endpoint compatibility.

    .. warning::
        ``dall-e-2`` / ``dall-e-3`` return temporary CDN URLs that expire
        after ~1 hour. Use ``ObjectStorageSink`` to upload assets immediately,
        or use a ``gpt-image-*`` model (saved locally from base64).

    Args:
        api_key: OpenAI API key. Falls back to ``OPENAI_API_KEY`` env var.
        http_timeout: HTTP request timeout in seconds (default 60).
        output_dir: Directory for saved images (default system temp). Also
            used as an allowed root for resolving ``file://`` edit inputs.
    """

    name = "openai-dalle"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return _build_dalle_registry()

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["image/png", "image/jpeg", "image/webp"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        http_timeout: float = 60.0,
        output_dir: str | Path | None = None,
        *,
        models: ModelRegistry | None = None,
    ):
        super().__init__(models=models)
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

    def _build_request_params(self, step: Step, spec: _ImageModelSpec) -> dict:
        """Assemble kwargs forwarded to client.images.generate/edit."""
        params: dict = {"model": step.model, "prompt": step.prompt or ""}
        forward_keys = (
            "size",
            "quality",
            "n",
            "style",
            "background",
            "output_format",
            "output_compression",
            "moderation",
            "input_fidelity",
        )
        for key in forward_keys:
            if key in step.params:
                params[key] = step.params[key]
        if "n" in params:
            params["n"] = int(params["n"])
        # response_format is only accepted by dall-e-*; gpt-image-* rejects it
        if spec.response_format == "url":
            params["response_format"] = "url"
        return params

    def _save_b64_image(self, b64_data: str, step: Step, index: int, ext: str) -> str:
        """Decode base64 image data and save to file. Returns file:// URI."""
        img_bytes = base64.b64decode(b64_data)
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}_{index}{ext}"
            out_path.write_bytes(img_bytes)
        else:
            fd, tmp = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            out_path = Path(tmp)
            out_path.write_bytes(img_bytes)
        return f"file://{quote(str(out_path.resolve()))}"

    def _materialize_inputs(self, step: Step) -> tuple[list[Path], list[Path]]:
        """Prepare edit image inputs. Returns (local_paths, temps_to_clean)."""
        local: list[Path] = []
        temps: list[Path] = []
        for asset in step.inputs:
            validate_chain_input_url(asset.url)
            parsed = urlparse(asset.url)
            if parsed.scheme == "file":
                local.append(_resolve_local_file(asset.url, self._output_dir))
            else:
                tmp = _download_https_to_temp(asset.url, self._http_timeout)
                temps.append(tmp)
                local.append(tmp)
        return local, temps

    def _resolve_mask(self, step: Step) -> tuple[Path | None, Path | None]:
        """Resolve optional mask URL. Returns (mask_path, temp_to_clean)."""
        mask_url = step.params.get("mask")
        if not mask_url:
            return None, None
        url_s = str(mask_url)
        validate_chain_input_url(url_s)
        parsed = urlparse(url_s)
        if parsed.scheme == "file":
            return _resolve_local_file(url_s, self._output_dir), None
        tmp = _download_https_to_temp(url_s, self._http_timeout)
        return tmp, tmp

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Generate or edit image(s). Routes by ``step.inputs`` presence."""
        client = self._get_client()
        spec = _MODELS.get(step.model, _DEFAULT_SPEC)
        _validate_params(step, spec)

        is_b64 = spec.response_format == "b64_json"
        is_edit = bool(step.inputs)
        out_fmt = str(step.params.get("output_format", "png"))
        ext = _FORMAT_TO_EXT.get(out_fmt, ".png")
        media_type = _FORMAT_TO_MEDIA.get(out_fmt, "image/png")

        params = self._build_request_params(step, spec)
        params.pop("mask", None)  # mask is handled via file handle, not dict

        temps_to_clean: list[Path] = []
        open_files: list[Any] = []
        try:
            if is_edit:
                local_paths, temps = self._materialize_inputs(step)
                temps_to_clean.extend(temps)
                mask_path, mask_temp = self._resolve_mask(step)
                if mask_temp is not None:
                    temps_to_clean.append(mask_temp)
                handles = [p.open("rb") for p in local_paths]
                open_files.extend(handles)
                params["image"] = handles[0] if len(handles) == 1 else handles
                if mask_path is not None:
                    mh = mask_path.open("rb")
                    open_files.append(mh)
                    params["mask"] = mh
                response = client.images.edit(**params)
            else:
                response = client.images.generate(**params)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"OpenAI image {'edit' if is_edit else 'generation'} failed: {exc}",
                error_code=map_openai_error(exc),
            ) from exc
        finally:
            for fh in open_files:
                with contextlib.suppress(Exception):
                    fh.close()
            for p in temps_to_clean:
                with contextlib.suppress(Exception):
                    p.unlink(missing_ok=True)

        for i, img in enumerate(response.data):
            if is_b64:
                b64 = getattr(img, "b64_json", None)
                if not b64:
                    continue
                step.assets.append(
                    Asset(url=self._save_b64_image(b64, step, i, ext), media_type=media_type)
                )
            else:
                url = getattr(img, "url", None)
                if url:
                    validate_asset_url(url)
                    step.assets.append(Asset(url=url, media_type=media_type))

        if not is_b64 and step.assets:
            logger.warning(
                "%s returns temporary URLs that expire (~1 hour). "
                "Use ObjectStorageSink to persist assets, or switch to a gpt-image-* model.",
                step.model,
            )

        self._apply_registry_pricing(step)
        return step
