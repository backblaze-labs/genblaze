"""Shared Replicate error mapping — used by provider.py."""

from genblaze_core.models.enums import ProviderErrorCode


def map_replicate_error(exc_or_msg: Exception | str) -> ProviderErrorCode:
    """Map a Replicate API exception (or error string) to a ProviderErrorCode."""
    msg = (str(exc_or_msg)).lower()
    if "rate" in msg or "429" in msg:
        return ProviderErrorCode.RATE_LIMIT
    if "auth" in msg or "401" in msg or "403" in msg or "token" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    if "invalid" in msg or "400" in msg or "validation" in msg:
        return ProviderErrorCode.INVALID_INPUT
    if "timeout" in msg or "timed out" in msg:
        return ProviderErrorCode.TIMEOUT
    if "model" in msg and ("not found" in msg or "error" in msg or "does not exist" in msg):
        return ProviderErrorCode.MODEL_ERROR
    if "500" in msg or "502" in msg or "503" in msg:
        return ProviderErrorCode.SERVER_ERROR
    return ProviderErrorCode.UNKNOWN
