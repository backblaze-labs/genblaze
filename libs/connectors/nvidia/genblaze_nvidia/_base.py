"""Shared HTTP + NVCF plumbing for all NVIDIA NIM providers.

NVIDIA exposes two public API surfaces on a single ``nvapi-`` key:

- ``integrate.api.nvidia.com/v1`` — OpenAI-compatible chat, embeddings,
  reranking. Always synchronous.
- ``ai.api.nvidia.com/v1/genai/{vendor}/{slug}`` — image / video / audio
  generation. Each endpoint may return either ``200`` with an inline payload
  (small/fast models) or ``202 + NVCF-REQID`` header (async, polled via
  ``api.nvcf.nvidia.com/v2/nvcf/pexec/status/{req_id}``).

This module owns the HTTP client, URL construction, and NVCF polling so the
per-modality provider classes only deal with payload shaping and response
parsing.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.retry import retry_after_from_response

from ._errors import map_nvidia_error

# Two different public base URLs — don't collapse them. Chat hits the
# OpenAI-compatible surface; image/video/audio generation hits the model-
# specific genai surface.
DEFAULT_CHAT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_GEN_BASE_URL = "https://ai.api.nvidia.com/v1"
DEFAULT_NVCF_STATUS_URL = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status"

# Env vars checked in order. NVIDIA_API_KEY is the documented primary;
# NVIDIA_NIM_API_KEY is a common alias some tutorials still recommend.
_API_KEY_ENV_VARS = ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY")

# HTTP statuses that mean "job queued, poll NVCF".
_NVCF_PENDING_STATUSES = frozenset({202})
_NVCF_DONE_STATUSES = frozenset({200})


def resolve_api_key(api_key: str | None) -> str | None:
    """Return the caller-supplied key, falling back to the documented env vars."""
    if api_key:
        return api_key
    for name in _API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def build_generation_path(model: str) -> str:
    """Build the path component for ``ai.api.nvidia.com/v1/genai/{model}``.

    NVIDIA's build.nvidia.com slugs are ``vendor/slug`` (e.g.
    ``stabilityai/stable-diffusion-xl``, ``nvidia/cosmos-1.0-7b-diffusion-text2world``).
    We construct the endpoint path as ``/genai/{model}`` — new models ship
    usable via pass-through without a code change.
    """
    model = model.strip("/")
    if not model:
        raise ProviderError(
            "Empty model id — cannot build NVIDIA generation URL",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    return f"/genai/{model}"


def decode_base64_payload(data: str, *, field: str = "base64") -> bytes:
    """Decode a base64 string from an NVIDIA response, raising on bad data.

    NIM returns image/video/audio outputs as base64 in either ``artifacts[*].base64``
    or top-level ``image`` / ``video`` / ``audio`` keys depending on the model.
    Pads the input to a multiple of 4 so endpoints that ship unpadded base64
    still decode — we still ``validate=True`` to catch genuinely corrupt data.
    """
    # Accept URL-safe base64 too — some endpoints use '-'/'_' instead of '+'/'/'.
    normalized = data.replace("-", "+").replace("_", "/")
    padding = (-len(normalized)) % 4
    try:
        return base64.b64decode(normalized + "=" * padding, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError(
            f"Failed to decode base64 {field!r} from NVIDIA response: {exc}",
            error_code=ProviderErrorCode.SERVER_ERROR,
        ) from exc


def extract_base64_assets(body: dict) -> list[tuple[bytes, str | None]]:
    """Pull ``(bytes, optional_mime)`` tuples from an NVIDIA generation response.

    Handles the shapes NIM currently emits:
    - ``{"artifacts": [{"base64": "...", "mime_type": "image/png"}, ...]}`` — SDXL / FLUX / SD 3.5
    - ``{"image": "..."}`` / ``{"video": "..."}`` / ``{"audio": "..."}`` — singleton convenience
    - ``{"data": [{"b64_json": "..."}, ...]}`` — some newer endpoints mirror OpenAI image shape

    Returns an empty list if no base64 payload is found — the caller decides
    whether that's an error.
    """
    out: list[tuple[bytes, str | None]] = []

    artifacts = body.get("artifacts")
    if isinstance(artifacts, list):
        for art in artifacts:
            if not isinstance(art, dict):
                continue
            b64 = art.get("base64") or art.get("b64_json")
            if isinstance(b64, str) and b64:
                mime = art.get("mime_type") or art.get("mimeType")
                out.append((decode_base64_payload(b64), mime if isinstance(mime, str) else None))
        if out:
            return out

    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json") or item.get("base64")
            if isinstance(b64, str) and b64:
                out.append((decode_base64_payload(b64), None))
        if out:
            return out

    # Singleton convenience keys — only check after the list-shaped forms so
    # multi-output responses aren't lossily collapsed to the first entry.
    for key, mime in (("image", None), ("video", None), ("audio", None)):
        val = body.get(key)
        if isinstance(val, str) and val:
            out.append((decode_base64_payload(val, field=key), mime))
            return out

    return out


def extract_asset_urls(body: dict) -> list[str]:
    """Pull hosted asset URLs from an NVIDIA response envelope, if any.

    Some gated endpoints (Cosmos, Edify) upload outputs to NIM's CDN and return
    signed URLs instead of inlining base64 — parse those too so callers don't
    need to care which shape the model returned.
    """
    urls: list[str] = []
    artifacts = body.get("artifacts")
    if isinstance(artifacts, list):
        for art in artifacts:
            if isinstance(art, dict):
                url = art.get("url") or art.get("signed_url") or art.get("download_url")
                if isinstance(url, str) and url:
                    urls.append(url)
    # Fallback: flat ``url`` / ``output_url`` keys used by some endpoints.
    for key in ("url", "output_url", "video_url", "image_url", "audio_url"):
        val = body.get(key)
        if isinstance(val, str) and val and val not in urls:
            urls.append(val)
    return urls


def unwrap_error_body(text: str) -> str:
    """Extract the inner ``{"detail"|"message"|"error": "..."}`` from a JSON body."""
    stripped = text.strip()
    if not stripped:
        return text
    try:
        body = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if isinstance(body, dict):
        detail = extract_error_detail(body)
        if detail:
            return detail
    return text


def extract_error_detail(body: dict) -> str:
    """Pull a human-readable error string out of a *parsed* NVIDIA error body.

    Callers should use this instead of stringifying the dict and running
    ``unwrap_error_body`` on the repr — stringifying a dict produces Python
    repr (single quotes), which isn't valid JSON, so round-tripping through
    ``unwrap_error_body`` returns the ugly repr unchanged.

    Lookup order: ``_raw`` (non-JSON fallback) → ``detail`` → ``message`` →
    ``error`` (both string and nested-``{"message": ...}`` forms). Falls back
    to ``json.dumps(body)`` so at least the user sees valid JSON rather than
    a Python repr when no known key matches.
    """
    if not body:
        return ""
    raw = body.get("_raw")
    if isinstance(raw, str) and raw:
        return unwrap_error_body(raw)
    for key in ("detail", "message", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("message") or value.get("detail")
            if isinstance(nested, str) and nested:
                return nested
    try:
        return json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(body)


def save_bytes_to_output_dir(
    payload: bytes,
    output_dir: Path | str | None,
    *,
    extension: str,
    prefix: str = "nvidia",
) -> str:
    """Write bytes to ``output_dir`` and return a ``file://`` URL.

    Used when NIM returns base64 — we persist to disk (SyncProviders write
    local files so chained downstream steps can read via ``file://``).
    When ``output_dir`` is None, write to the current working directory.
    """
    base = Path(output_dir) if output_dir is not None else Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}-{uuid.uuid4().hex[:12]}.{extension.lstrip('.')}"
    path = base / filename
    path.write_bytes(payload)
    return path.resolve().as_uri()


class NvidiaClient:
    """HTTP client shared across NVIDIA provider classes.

    Owns a single ``httpx.Client`` configured with the ``nvapi-`` Bearer header.
    Providers construct one per instance (or share via injection for tests).

    Args:
        api_key: NVIDIA API key. Falls back to ``NVIDIA_API_KEY`` /
            ``NVIDIA_NIM_API_KEY`` env vars. Ignored when ``http_client`` is
            injected.
        gen_base_url: Override the image/video/audio generation base URL.
            Also falls back to ``NVIDIA_GEN_BASE_URL`` env var — useful for
            self-hosted NIM deployments.
        chat_base_url: Override the chat/embeddings base URL. Falls back to
            ``NVIDIA_CHAT_BASE_URL`` env var.
        nvcf_status_url: Override the NVCF async-status base URL. Falls back
            to ``NVIDIA_NVCF_STATUS_URL`` env var.
        http_timeout: Default HTTP request timeout in seconds.
        http_client: Pre-built ``httpx.Client`` to inject. When supplied, the
            base URL and auth header are the caller's responsibility and all
            base-URL / API-key overrides on this class are ignored.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        gen_base_url: str | None = None,
        chat_base_url: str | None = None,
        nvcf_status_url: str | None = None,
        http_timeout: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = resolve_api_key(api_key)
        self._gen_base_url = (
            gen_base_url or os.environ.get("NVIDIA_GEN_BASE_URL") or DEFAULT_GEN_BASE_URL
        ).rstrip("/")
        self._chat_base_url = (
            chat_base_url or os.environ.get("NVIDIA_CHAT_BASE_URL") or DEFAULT_CHAT_BASE_URL
        ).rstrip("/")
        self._nvcf_status_url = (
            nvcf_status_url or os.environ.get("NVIDIA_NVCF_STATUS_URL") or DEFAULT_NVCF_STATUS_URL
        ).rstrip("/")
        self._http_timeout = http_timeout
        self._http_client = http_client
        self._owns_client = http_client is None

    # --- lifecycle ---

    def http(self) -> httpx.Client:
        """Lazy-create (or return injected) httpx client for generation endpoints."""
        if self._http_client is None:
            if not self._api_key:
                raise ProviderError(
                    "No NVIDIA API key found. Set NVIDIA_API_KEY env var or pass api_key=.",
                    error_code=ProviderErrorCode.AUTH_FAILURE,
                )
            self._http_client = httpx.Client(
                base_url=self._gen_base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                },
                timeout=self._http_timeout,
            )
            self._owns_client = True
        return self._http_client

    def close(self) -> None:
        """Release connection-pool resources for internally-created clients."""
        if self._http_client is not None and self._owns_client:
            self._http_client.close()
            self._http_client = None

    # --- accessors (needed by subclasses that construct per-call URLs) ---

    @property
    def chat_base_url(self) -> str:
        return self._chat_base_url

    @property
    def api_key(self) -> str | None:
        return self._api_key

    # --- generation POST ---

    def post_generation(self, model: str, payload: dict) -> tuple[int, dict, dict]:
        """POST a generation request and return ``(status_code, body, headers)``.

        ``body`` is the parsed JSON (or an ``{"_raw": bytes}`` fallback if the
        response wasn't JSON — uncommon but guarded against). ``headers`` is a
        lowercase-keyed dict to make ``NVCF-REQID`` lookup case-insensitive.

        Raises ``ProviderError`` on transport failures; returns raw responses
        on HTTP errors so callers can inspect the body for retry classification.
        """
        client = self.http()
        path = build_generation_path(model)
        try:
            resp = client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"NVIDIA submit failed (transport): {exc}",
                error_code=map_nvidia_error(exc),
            ) from exc

        headers = {k.lower(): v for k, v in resp.headers.items()}
        body = self._parse_json_body(resp)
        return resp.status_code, body, headers

    def poll_nvcf(self, request_id: str) -> tuple[int, dict, dict]:
        """Poll NVCF for an async job. Returns ``(status_code, body, headers)``.

        A 200 means the job is complete; a 202 means still running. Any other
        status is an error and surfaces via the returned body for the caller
        to classify. Headers are lowercase-keyed so callers can read
        ``retry-after`` case-insensitively.
        """
        if not request_id:
            raise ProviderError(
                "Empty NVCF request id — cannot poll",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        client = self.http()
        # NVCF status URL is on a *different* host than the gen client's
        # base_url — pass the absolute URL so httpx bypasses base_url joining.
        url = f"{self._nvcf_status_url}/{request_id}"
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"NVIDIA poll failed (transport): {exc}",
                error_code=map_nvidia_error(exc),
            ) from exc
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status_code, self._parse_json_body(resp), headers

    def _parse_json_body(self, resp: httpx.Response) -> dict:
        """Parse JSON; fall back to a dict wrapping raw text for non-JSON bodies."""
        content_type = resp.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            return {"_raw": resp.text}
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return {"_raw": resp.text}

    def wait_for_nvcf(
        self,
        request_id: str,
        *,
        timeout: float = 600.0,
        initial_interval: float = 5.0,
    ) -> dict:
        """Block until an NVCF job completes; return the final body.

        Used by SyncProvider subclasses (image, audio) to hide async-ness
        from callers who expect a single blocking call. The interval doubles
        every 30 seconds of elapsed time (matching the base class's
        ``_adaptive_poll_interval``) but here we're in a simple function so we
        inline the same formula.
        """
        start = time.monotonic()
        while True:
            status, body, headers = self.poll_nvcf(request_id)
            if status == 200:
                return body
            if status in _NVCF_PENDING_STATUSES:
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    raise ProviderError(
                        f"NVCF poll timeout after {elapsed:.1f}s (limit: {timeout}s)",
                        error_code=ProviderErrorCode.TIMEOUT,
                    )
                doublings = int(elapsed / 30)
                sleep_for = min(initial_interval * (2**doublings), 30.0)
                time.sleep(sleep_for)
                continue
            detail = extract_error_detail(body)
            raise ProviderError(
                f"NVIDIA NVCF status {status}: {detail}",
                error_code=map_nvidia_error(Exception(detail), status),
                retry_after=retry_after_from_response(headers),
            )
