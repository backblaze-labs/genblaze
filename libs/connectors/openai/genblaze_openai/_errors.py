"""Shared OpenAI error mapping — used by dalle.py, provider.py, tts.py."""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_openai_error(exc: Exception) -> ProviderErrorCode:
    """Map an OpenAI API exception to a ProviderErrorCode."""
    return classify_api_error(exc)
