"""Error mapping for the Atlas Cloud connector."""

from __future__ import annotations

import httpx
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_atlascloud_error(exc: Exception) -> ProviderErrorCode:
    """Map Atlas Cloud and transport failures to genblaze error codes."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderErrorCode.TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return ProviderErrorCode.AUTH_FAILURE
        if status == 429:
            return ProviderErrorCode.RATE_LIMIT
        if status in (400, 422):
            return ProviderErrorCode.INVALID_INPUT
        if status >= 500:
            return ProviderErrorCode.SERVER_ERROR
    return classify_api_error(exc)
