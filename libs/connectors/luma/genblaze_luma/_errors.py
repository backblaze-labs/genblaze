"""Shared Luma error mapping — used by provider.py."""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_luma_error(exc: Exception) -> ProviderErrorCode:
    """Map a Luma API exception to a ProviderErrorCode."""
    return classify_api_error(exc)
