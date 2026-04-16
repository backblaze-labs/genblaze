"""Shared Google API error mapping — used by provider.py, imagen.py."""

from genblaze_core.models.enums import ProviderErrorCode


def map_google_error(exc: Exception) -> ProviderErrorCode:
    """Map a Google API exception to a ProviderErrorCode."""
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg or "resource_exhausted" in msg:
        return ProviderErrorCode.RATE_LIMIT
    if "auth" in msg or "401" in msg or "403" in msg or "permission" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    if "invalid" in msg or "400" in msg:
        return ProviderErrorCode.INVALID_INPUT
    if "timeout" in msg or "deadline" in msg:
        return ProviderErrorCode.TIMEOUT
    if "500" in msg or "unavailable" in msg or "internal" in msg:
        return ProviderErrorCode.SERVER_ERROR
    return ProviderErrorCode.UNKNOWN
