"""Error mapping for the fal.ai connector.

fal returns two documented error body shapes:

- Model errors (https://fal.ai/docs/documentation/model-apis/errors):
  ``{"detail": [{"loc": [...], "msg": "...", "type": "content_policy_violation"}]}``
- Request errors (https://fal.ai/docs/documentation/model-apis/request-errors):
  ``{"detail": "Request timed out", "error_type": "request_timeout"}``, with the
  same value mirrored in the ``X-Fal-Error-Type`` response header.

The machine-readable ``type`` / ``error_type`` wins over the HTTP status so a
422 content-policy refusal is not misreported as a plain invalid input.
"""

from __future__ import annotations

from typing import Any

import httpx
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error

# Bound the upstream text surfaced in ProviderError messages; fal's model
# errors can echo the full request input back.
_MAX_DETAIL_CHARS = 500

_TYPE_CODES: dict[str, ProviderErrorCode] = {
    "content_policy_violation": ProviderErrorCode.CONTENT_POLICY,
    "no_media_generated": ProviderErrorCode.MODEL_ERROR,
    "generation_timeout": ProviderErrorCode.TIMEOUT,
    "request_timeout": ProviderErrorCode.TIMEOUT,
    "startup_timeout": ProviderErrorCode.TIMEOUT,
    "bad_request": ProviderErrorCode.INVALID_INPUT,
}


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_types(response: httpx.Response) -> list[str]:
    """Collect every machine-readable fal error type on a response."""
    types: list[str] = []
    header = response.headers.get("x-fal-error-type")
    if header:
        types.append(header)
    body = _json_body(response)
    if isinstance(body, dict):
        if isinstance(body.get("error_type"), str):
            types.append(body["error_type"])
        detail = body.get("detail")
        if isinstance(detail, list):
            types.extend(
                item["type"]
                for item in detail
                if isinstance(item, dict) and isinstance(item.get("type"), str)
            )
    return types


def describe_fal_error(response: httpx.Response) -> str:
    """Human-readable summary of a fal error response (bounded length)."""
    body = _json_body(response)
    detail: Any = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        parts = [
            f"{item.get('type', 'error')}: {item.get('msg', '')}".strip()
            for item in detail
            if isinstance(item, dict)
        ]
        text = "; ".join(parts)
    elif isinstance(detail, str):
        text = detail
    else:
        text = response.text
    text = text or response.reason_phrase or "unknown error"
    return f"HTTP {response.status_code}: {text[:_MAX_DETAIL_CHARS]}"


def map_fal_error_type(error_type: str | None) -> ProviderErrorCode | None:
    """Map a fal ``error_type`` / ``detail[].type`` string, or None if unmapped."""
    if not error_type:
        return None
    if error_type in _TYPE_CODES:
        return _TYPE_CODES[error_type]
    if error_type.startswith("runner_") or error_type == "internal_error":
        return ProviderErrorCode.SERVER_ERROR
    return None


def map_fal_error(exc: Exception) -> ProviderErrorCode:
    """Map fal and transport failures to genblaze error codes."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderErrorCode.TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        for error_type in _error_types(exc.response):
            code = map_fal_error_type(error_type)
            if code is not None:
                return code
        status = exc.response.status_code
        if status in (401, 403):
            return ProviderErrorCode.AUTH_FAILURE
        if status == 404:
            # Unknown model slug on submit, or an unknown request id on poll.
            return ProviderErrorCode.MODEL_ERROR
        if status == 429:
            return ProviderErrorCode.RATE_LIMIT
        if status in (400, 422):
            return ProviderErrorCode.INVALID_INPUT
        if status == 504:
            return ProviderErrorCode.TIMEOUT
        if status >= 500:
            return ProviderErrorCode.SERVER_ERROR
    return classify_api_error(exc)
