"""Shared GMICloud error mapping — used by provider.py."""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_gmicloud_error(exc: Exception) -> ProviderErrorCode:
    """Map a GMICloud API exception to a ProviderErrorCode."""
    # Check for auth-related errors from the JWT session flow
    msg = str(exc).lower()
    if "unauthorized" in msg or "invalid credentials" in msg or "401" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    if "forbidden" in msg or "403" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    return classify_api_error(exc)
