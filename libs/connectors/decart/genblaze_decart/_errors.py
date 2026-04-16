"""Shared Decart error mapping — used by provider.py, image.py."""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_decart_error(exc: Exception) -> ProviderErrorCode:
    """Map a Decart API exception to a ProviderErrorCode."""
    return classify_api_error(exc)
