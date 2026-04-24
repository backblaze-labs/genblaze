"""Shared GMICloud error mapping — used by all GMICloud providers."""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_gmicloud_error(exc: Exception, status_code: int | None = None) -> ProviderErrorCode:
    """Map a GMICloud API exception to a ProviderErrorCode.

    Args:
        exc: The exception that occurred.
        status_code: HTTP status code if available (httpx API path).
    """
    msg = str(exc).lower()
    # Content-policy / safety refusal wins over status-code triage — GMICloud
    # returns a 400 for policy rejections, which would otherwise be
    # misclassified as a generic INVALID_INPUT retry candidate.
    if (
        "content_policy" in msg
        or "content policy" in msg
        or "safety" in msg
        or "policy violation" in msg
    ):
        return ProviderErrorCode.CONTENT_POLICY
    # HTTP status codes take priority (httpx / REST API path)
    if status_code == 429:
        return ProviderErrorCode.RATE_LIMIT
    if status_code in (401, 403):
        return ProviderErrorCode.AUTH_FAILURE
    if status_code == 400:
        return ProviderErrorCode.INVALID_INPUT
    if status_code and status_code >= 500:
        return ProviderErrorCode.SERVER_ERROR
    # String-based fallback for SDK exceptions
    if "unauthorized" in msg or "invalid credentials" in msg or "401" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    if "forbidden" in msg or "403" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    return classify_api_error(exc)
