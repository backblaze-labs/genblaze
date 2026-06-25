"""Shared AssemblyAI error mapping — used by provider.py.

The AssemblyAI Python SDK raises exceptions that may carry an HTTP
``status_code`` attribute (e.g. ``aai.types.TranscriptError`` and the
underlying transport errors). We classify off the status code when present,
then fall back to the shared string-based classifier. The same function also
classifies the plain ``transcript.error`` string returned when a transcript
finishes with ``status == "error"``.
"""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_assemblyai_error(exc: Exception | str) -> ProviderErrorCode:
    """Map an AssemblyAI API exception (or error string) to a ProviderErrorCode."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return ProviderErrorCode.RATE_LIMIT
        if status in (401, 403):
            return ProviderErrorCode.AUTH_FAILURE
        if status in (400, 422):
            return ProviderErrorCode.INVALID_INPUT
        if status >= 500:
            return ProviderErrorCode.SERVER_ERROR
    # Fall back to shared string-based classifier (also handles the plain
    # ``transcript.error`` string from a failed transcript).
    return classify_api_error(exc)
