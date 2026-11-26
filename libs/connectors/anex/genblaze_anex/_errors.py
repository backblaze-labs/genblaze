"""Error mapping for the anex.sh connector.

The task API answers errors in one documented shape
(https://anex.sh/docs/video#errors)::

    {"type": "error",
     "error": {"type": "rate_limit_error", "message": "... (1045)", "http_code": "429"},
     "request_id": "..."}

A task that was accepted and failed later carries ``error.code`` and
``error.message`` on the task object instead (``content_policy_violation``,
``media_fetch_failed``, ...).

The machine-readable ``error.type`` wins over the HTTP status, so a 422
screening refusal is reported as a content-policy refusal and not as a plain
invalid input.
"""

from __future__ import annotations

from typing import Any

import httpx
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error

# Bound the upstream text surfaced in ProviderError messages.
_MAX_DETAIL_CHARS = 500

_TYPE_CODES: dict[str, ProviderErrorCode] = {
    "authorized_error": ProviderErrorCode.AUTH_FAILURE,
    # No credit left. There is no billing code, and this is an account-level
    # refusal that a retry cannot fix, so it shares the non-retryable auth code.
    "insufficient_balance_error": ProviderErrorCode.AUTH_FAILURE,
    "rate_limit_error": ProviderErrorCode.RATE_LIMIT,
    # 422: the request was refused by content screening.
    "unprocessable_entity_error": ProviderErrorCode.CONTENT_POLICY,
    "bad_request_error": ProviderErrorCode.INVALID_INPUT,
    "not_found_error": ProviderErrorCode.INVALID_INPUT,
    "server_error": ProviderErrorCode.SERVER_ERROR,
}

# ``error.code`` values of a task that failed after it was accepted.
_TASK_CODE_PREFIX_MEDIA = "media_"
_TASK_CODE_CONTENT_POLICY = "content_policy_violation"


def _error_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def describe_anex_error(response: httpx.Response) -> str:
    """Human-readable summary of an error response (bounded length)."""
    error = _error_object(response)
    message = error.get("message")
    text = message if isinstance(message, str) and message else response.text
    text = text or response.reason_phrase or "unknown error"
    return f"HTTP {response.status_code}: {text[:_MAX_DETAIL_CHARS]}"


def map_anex_task_error(code: str | None) -> ProviderErrorCode:
    """Map the ``error.code`` of a failed task. A failed task is terminal."""
    if code == _TASK_CODE_CONTENT_POLICY:
        return ProviderErrorCode.CONTENT_POLICY
    if code and code.startswith(_TASK_CODE_PREFIX_MEDIA):
        # A supplied image, audio file or video was refused or unreadable.
        return ProviderErrorCode.INVALID_INPUT
    return ProviderErrorCode.MODEL_ERROR


def map_anex_error(exc: Exception) -> ProviderErrorCode:
    """Map anex.sh and transport failures to genblaze error codes."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderErrorCode.TIMEOUT
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        # Transient transport failure: retryable for the idempotent GETs.
        # Submit never retries it (see AnexProvider.submit).
        return ProviderErrorCode.SERVER_ERROR
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 408:
            # A large request body was not received in time.
            return ProviderErrorCode.TIMEOUT
        error_type = _error_object(exc.response).get("type")
        if isinstance(error_type, str) and error_type in _TYPE_CODES:
            return _TYPE_CODES[error_type]
        if status in (401, 402, 403):
            return ProviderErrorCode.AUTH_FAILURE
        if status == 429:
            return ProviderErrorCode.RATE_LIMIT
        if status == 422:
            return ProviderErrorCode.CONTENT_POLICY
        if 400 <= status < 500:
            return ProviderErrorCode.INVALID_INPUT
        if status >= 500:
            return ProviderErrorCode.SERVER_ERROR
    return classify_api_error(exc)
