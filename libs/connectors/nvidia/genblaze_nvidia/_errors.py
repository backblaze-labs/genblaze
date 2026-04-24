"""Shared NVIDIA NIM error mapping — used by all NVIDIA providers.

NIM returns safety refusals as HTTP 400 with a ``Nemoguard`` / ``safety`` /
``content policy`` marker in the body. That must classify as CONTENT_POLICY
(non-retryable) rather than a generic INVALID_INPUT — otherwise the caller
will burn retries on a deterministic refusal.
"""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_nvidia_error(exc: Exception, status_code: int | None = None) -> ProviderErrorCode:
    """Map an NVIDIA NIM API exception / HTTP response to a ProviderErrorCode.

    Args:
        exc: The exception that occurred (or ``Exception(body_text)`` when
            surfaced from an httpx response).
        status_code: HTTP status code when available — takes priority over
            string matching for unambiguous signals (401/403/429/5xx).
    """
    msg = str(exc).lower()
    # Content-policy / safety refusal wins over status-code triage. NIM marks
    # these with "nemoguard", "guardrail", or the standard "content policy" /
    # "safety filter" terms. Classifying as CONTENT_POLICY prevents pointless
    # retries on deterministic refusals.
    if (
        "nemoguard" in msg
        or "guardrail" in msg
        or "content_policy" in msg
        or "content policy" in msg
        or "safety filter" in msg
        or "safety_filter" in msg
        or "policy violation" in msg
        or "blocked by safety" in msg
    ):
        return ProviderErrorCode.CONTENT_POLICY
    # HTTP status codes take priority over message string-matching.
    if status_code == 429:
        return ProviderErrorCode.RATE_LIMIT
    if status_code in (401, 403):
        return ProviderErrorCode.AUTH_FAILURE
    if status_code == 404:
        return ProviderErrorCode.MODEL_ERROR
    if status_code == 400:
        return ProviderErrorCode.INVALID_INPUT
    if status_code and status_code >= 500:
        return ProviderErrorCode.SERVER_ERROR
    # String-based fallback for SDK exceptions / transport errors.
    return classify_api_error(exc)
