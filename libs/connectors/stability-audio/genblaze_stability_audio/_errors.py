"""Shared Stability Audio error mapping — used by provider.py."""

from genblaze_core.models.enums import ProviderErrorCode


def map_stability_audio_error(
    exc: Exception, status_code: int | None = None
) -> ProviderErrorCode:
    """Map a Stability API error to a ProviderErrorCode.

    Args:
        exc: The exception that occurred.
        status_code: HTTP status code if available (raw HTTP API).
    """
    if status_code == 429:
        return ProviderErrorCode.RATE_LIMIT
    if status_code in (401, 403):
        return ProviderErrorCode.AUTH_FAILURE
    if status_code == 400:
        return ProviderErrorCode.INVALID_INPUT
    if status_code and status_code >= 500:
        return ProviderErrorCode.SERVER_ERROR
    msg = str(exc).lower()
    if "timeout" in msg:
        return ProviderErrorCode.TIMEOUT
    return ProviderErrorCode.UNKNOWN
