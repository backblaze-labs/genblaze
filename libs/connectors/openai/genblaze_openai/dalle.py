"""OpenAI image provider — adapter for /v1/images/generations and /v1/images/edits.

Supports OpenAI's full image model lineup: ``gpt-image-2``, ``gpt-image-1.5``,
``gpt-image-1``, ``gpt-image-1-mini``, ``dall-e-3``, ``dall-e-2``. Unknown /
alias models (e.g. ``chatgpt-image-latest``, dated snapshots) pass through
with ``cost_usd=None``.

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships two
pattern-keyed ``ModelFamily`` rules (gpt-image and dall-e) plus
``DiscoverySupport.NATIVE`` discovery via ``client.models.list()``
(filtered to image-shaped slugs).

**Pricing**: previously a complex ``(quality, size) → USD`` table per
model variant. As of 0.3.0 the SDK no longer ships pricing — see
``docs/reference/pricing-recipes.md`` for the canonical recipe. The
per-model ``_MODELS`` config below remains as connector-internal
validation state (size enums, quality enums, fixed_sizes, response
format) but no longer carries pricing.

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
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse
from urllib.request import url2pathname

from genblaze_core._utils import open_pinned_https_connection
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
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
    SyncProvider,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.providers.discovery import DEFAULT_TTL_SECONDS, _DiscoveryCache
from genblaze_core.providers.retry import retry_after_from_response
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
    """Per-model connector-internal validation config.

    Drives the bespoke ``_validate_params`` logic for size/quality enums
    and the response-format dispatch (b64_json vs CDN URL). Pricing
    moved to ``docs/reference/pricing-recipes.md`` as of
    ``genblaze-core 0.3.0``.

    ``supports_input_fidelity`` is advisory — a mismatch emits a warning
    and still forwards the param. The server is the authority for
    capability rejection.
    """

    response_format: Literal["b64_json", "url"]
    valid_qualities: frozenset[str]
    fixed_sizes: frozenset[str] | None  # None => free-form (gpt-image-2)
    supports_input_fidelity: bool


# Permissive fallback for unknown/alias models (chatgpt-image-latest, snapshots).
_DEFAULT_SPEC = _ImageModelSpec(
    response_format="b64_json",
    valid_qualities=_GPT_QUALITIES,
    fixed_sizes=None,
    supports_input_fidelity=True,
)


_MODELS: dict[str, _ImageModelSpec] = {
    "gpt-image-2": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=None,  # free-form; see _validate_gpt_image_2_size
        supports_input_fidelity=False,  # native HF — param is no-op server-side
    ),
    "gpt-image-1.5": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=_GPT_IMAGE_FIXED_SIZES,
        supports_input_fidelity=True,
    ),
    "gpt-image-1": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=_GPT_IMAGE_FIXED_SIZES,
        supports_input_fidelity=True,
    ),
    "gpt-image-1-mini": _ImageModelSpec(
        response_format="b64_json",
        valid_qualities=_GPT_QUALITIES,
        fixed_sizes=_GPT_IMAGE_FIXED_SIZES,
        supports_input_fidelity=False,  # per openai-python SDK reference
    ),
    "dall-e-3": _ImageModelSpec(
        response_format="url",
        valid_qualities=_DALLE3_QUALITIES,
        fixed_sizes=_DALLE3_SIZES,
        supports_input_fidelity=False,
    ),
    "dall-e-2": _ImageModelSpec(
        response_format="url",
        valid_qualities=_DALLE2_QUALITIES,
        fixed_sizes=_DALLE2_SIZES,
        supports_input_fidelity=False,
    ),
}


# Two families: gpt-image-* and dall-e-*. Both share the IMAGE modality
# but ship different param-shape contracts (size enum, response format)
# enforced by ``_validate_params`` against ``_MODELS[model_id]`` at submit
# time. Family-level constraints are intentionally absent — the bespoke
# validation in ``_validate_params`` owns them.
_OPENAI_GPT_IMAGE_FAMILY = ModelFamily(
    name="openai-gpt-image",
    pattern=re.compile(r"^gpt-image-"),
    spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
    description="OpenAI gpt-image-* family — latest image generation models.",
    example_slugs=("gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2"),
)

_OPENAI_DALLE_FAMILY = ModelFamily(
    name="openai-dalle",
    pattern=re.compile(r"^dall-e-"),
    spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
    description="OpenAI DALL-E family (legacy) — dall-e-2 and dall-e-3.",
    example_slugs=("dall-e-2", "dall-e-3"),
)


_FALLBACK = ModelSpec(model_id="*", modality=Modality.IMAGE)


def _build_dalle_registry() -> ModelRegistry:
    """Build the registry with two families covering the OpenAI image surface.

    Parameter validation stays connector-side via ``_validate_params``
    (free-form size rules for gpt-image-2, advisory input_fidelity
    warnings). The registry surfaces:
    - Which slugs the connector handles (for capability discovery).
    - NATIVE-discovery integration via ``client.models.list()``.
    """
    return ModelRegistry(
        provider_families=(_OPENAI_GPT_IMAGE_FAMILY, _OPENAI_DALLE_FAMILY),
        fallback=_FALLBACK,
    )


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
    # url2pathname handles Windows drive letters: /C:/... → C:\... (no-op on Unix)
    resolved = Path(url2pathname(parsed.path)).resolve()
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
    """Download an https:// URL to a temp file. SSRF-checked with DNS pinning.

    Uses ``open_pinned_https_connection`` which connects to the validated pinned
    IP rather than letting the HTTP client re-resolve the hostname. This closes
    the DNS rebinding / TOCTOU window. TLS SNI and cert verification still use
    the original hostname. http.client has no redirect handler, so a 3xx raises.

    Note: outbound connections bypass HTTP(S)_PROXY / NO_PROXY env vars by
    design — see ``open_pinned_https_connection`` for rationale.

    Caller is responsible for unlinking the returned temp file.
    """
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host = parsed.hostname or ""

    fd, tmp = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    tmp_path = Path(tmp)
    conn = None
    try:
        conn = open_pinned_https_connection(url, timeout=timeout, exc_type=ProviderError)
        conn.request("GET", path, headers={"User-Agent": "genblaze-openai", "Host": host})
        resp = conn.getresponse()
        if resp.status >= 300:
            resp.read()
            raise ProviderError(
                f"HTTP {resp.status} downloading image from {url}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        size = 0
        with tmp_path.open("wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_INPUT_BYTES:
                    raise ProviderError(
                        f"Image download exceeds {_MAX_INPUT_BYTES} byte limit",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                f.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if conn is not None:
            conn.close()
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

    .. note::
        ``dall-e-2`` / ``dall-e-3`` return short-lived, credential-bearing CDN
        URLs (Azure SAS, ~1 hour). The provider downloads them immediately to a
        local ``file://`` asset with a populated ``sha256``, so outputs are
        durable and verifiable; the signed URL is used only for that fetch and
        is never persisted. Use ``ObjectStorageSink`` to upload to object
        storage.

    Args:
        api_key: OpenAI API key. Falls back to ``OPENAI_API_KEY`` env var.
        http_timeout: HTTP request timeout in seconds (default 60).
        output_dir: Directory for saved images (default system temp). Also
            used as an allowed root for resolving ``file://`` edit inputs.
    """

    name = "openai-dalle"
    discovery_support = DiscoverySupport.NATIVE
    """OpenAI exposes ``client.models.list()`` as the authoritative
    catalog. The fetcher filters to image-shaped slugs (``gpt-image-*``
    and ``dall-e-*``) so chat / TTS / Sora slugs don't pollute the
    DALL-E provider's cache."""

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
        self._http_timeout = http_timeout
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None
        # Wire NATIVE discovery — fetcher closes over self.
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_models,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    # --- catalog discovery (DiscoverySupport.NATIVE) ----------------------

    def _fetch_models(self) -> DiscoveryResult:
        """Fetch /v1/models, filter to image-shaped slugs."""
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
        """Snapshot the image-filtered OpenAI catalog. Single-flight, TTL-bounded."""
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

    def _persist_image_bytes(
        self, img_bytes: bytes, step: Step, index: int, ext: str
    ) -> tuple[str, str, int]:
        """Write image bytes to a local file and hash them.

        Returns ``(file_uri, sha256_hex, size_bytes)``. Output assets carry a
        content hash so manifests verify without a storage sink, and
        ``ObjectStorageSink`` can reuse the hash/size on transfer retry.
        """
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{step.step_id}_{index}{ext}"
        else:
            fd, tmp = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            out_path = Path(tmp)
        out_path.write_bytes(img_bytes)
        return (
            f"file://{quote(str(out_path.resolve()))}",
            hashlib.sha256(img_bytes).hexdigest(),
            len(img_bytes),
        )

    def _download_url_bytes(self, url: str) -> bytes:
        """Fetch a provider-returned output URL via the SSRF-pinned downloader.

        ``dall-e-2`` / ``dall-e-3`` return short-lived, credential-bearing CDN
        URLs (Azure SAS). We fetch the bytes immediately so the asset is durable
        and hashable; the signed URL is used only for this fetch and is never
        persisted into the manifest, cache, or any sink.
        """
        tmp = _download_https_to_temp(url, self._http_timeout)
        try:
            return tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)

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
                retry_after=retry_after_from_response(exc),
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
                img_bytes = base64.b64decode(b64)
            else:
                remote_url = getattr(img, "url", None)
                if not remote_url:
                    continue
                # Fetch the short-lived signed URL now; the credentialed URL is
                # never persisted (see _download_url_bytes).
                validate_asset_url(remote_url)
                img_bytes = self._download_url_bytes(remote_url)
            uri, sha256, size = self._persist_image_bytes(img_bytes, step, i, ext)
            step.assets.append(
                Asset(url=uri, media_type=media_type, sha256=sha256, size_bytes=size)
            )

        self._apply_registry_pricing(step)
        return step
