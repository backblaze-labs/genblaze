"""Shared ElevenLabs error mapping — used by provider.py, sfx.py."""

from genblaze_core.models.enums import ProviderErrorCode


def map_elevenlabs_error(exc: Exception) -> ProviderErrorCode:
    """Map an ElevenLabs API exception to a ProviderErrorCode."""
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg:
        return ProviderErrorCode.RATE_LIMIT
    if "auth" in msg or "401" in msg or "403" in msg or "api_key" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    if "invalid" in msg or "400" in msg or "422" in msg:
        return ProviderErrorCode.INVALID_INPUT
    if "timeout" in msg or "timed out" in msg:
        return ProviderErrorCode.TIMEOUT
    if "500" in msg or "502" in msg or "503" in msg:
        return ProviderErrorCode.SERVER_ERROR
    return ProviderErrorCode.UNKNOWN
