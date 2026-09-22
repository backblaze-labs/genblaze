"""MuAPI HTTP and terminal-result error classification."""

from __future__ import annotations

from typing import Any

import httpx
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error

_MAX_DETAIL_CHARS = 500


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def describe_muapi_error(response: httpx.Response) -> str:
    """Return a bounded, useful description of a MuAPI HTTP error."""
    body = _json_body(response)
    detail: Any = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error") or detail.get("code")
    if isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail)
    text = detail or (body.get("error") if isinstance(body, dict) else None) or response.text
    text = str(text or response.reason_phrase or "unknown error")
    return f"HTTP {response.status_code}: {text[:_MAX_DETAIL_CHARS]}"


def map_muapi_error(exc: Exception) -> ProviderErrorCode:
    """Map MuAPI transport/HTTP failures to genblaze's retry taxonomy."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderErrorCode.TIMEOUT
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return ProviderErrorCode.SERVER_ERROR
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return ProviderErrorCode.AUTH_FAILURE
        if status == 402:
            return ProviderErrorCode.AUTH_FAILURE
        if status == 404:
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


def map_muapi_result_error(message: str) -> ProviderErrorCode:
    """Classify an error returned in a completed prediction envelope."""
    return classify_api_error(message)
