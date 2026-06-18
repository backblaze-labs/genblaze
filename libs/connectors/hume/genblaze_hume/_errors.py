"""Shared Hume error mapping — used by provider.py.

The Hume Python SDK is Fern-generated and raises ``ApiError`` subclasses
carrying a ``status_code`` attribute. We classify off the status code when
present, then fall back to the shared string-based classifier.
"""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_hume_error(exc: Exception) -> ProviderErrorCode:
    """Map a Hume API exception to a ProviderErrorCode."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return ProviderErrorCode.RATE_LIMIT
        if status in (401, 403):
            return ProviderErrorCode.AUTH_FAILURE
        if status in (400, 422):
            return ProviderErrorCode.INVALID_INPUT
        if status in (500, 502, 503, 504):
            return ProviderErrorCode.SERVER_ERROR
    # Fall back to shared string-based classifier.
    return classify_api_error(exc)
