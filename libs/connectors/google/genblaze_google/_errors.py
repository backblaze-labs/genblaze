"""Shared Google API error mapping — used by provider.py, imagen.py,
chat.py, gemini_image.py."""

from genblaze_core.models.enums import ProviderErrorCode


def map_google_error(exc: Exception) -> ProviderErrorCode:
    """Map a Google API exception to a ProviderErrorCode."""
    msg = str(exc).lower()
    # Imagen entitlement gate (issue #206): models.get says the slug is
    # cataloged, but :predict 404s for accounts without Imagen access.
    # ImagenProvider.validate_model warns (OK_PROVISIONAL) rather than blocks
    # at preflight, so an unentitled imagen-4.0-* slug is expected to reach
    # call time and 404 on the actual API call — this branch, not preflight,
    # is where the gate is actually handled. MODEL_ERROR matches what
    # preflight already raises for a DEAD probe result, so the pipeline's
    # fallback_models retry fires on it exactly like any other dead slug.
    if "no longer available to new users" in msg:
        return ProviderErrorCode.MODEL_ERROR
    # Bare "rate" collides with "generate"/"generateContent" (this call's own
    # method name shows up in most SDK error strings) — match on "rate limit" /
    # "rate_limit" instead, same predicate genblaze_core's own classify_api_error
    # uses (providers/base.py).
    if "rate limit" in msg or "rate_limit" in msg or "429" in msg or "resource_exhausted" in msg:
        return ProviderErrorCode.RATE_LIMIT
    # Gemini / Imagen safety block — deterministic refusal, never retryable.
    # Surfaces as "safety", "blocked", "responsibleai", or "content_filter"
    # depending on the SDK code path.
    if (
        "safety" in msg
        or "blocked" in msg
        or "responsibleai" in msg
        or "content_filter" in msg
        or "content filter" in msg
    ):
        return ProviderErrorCode.CONTENT_POLICY
    if "auth" in msg or "401" in msg or "403" in msg or "permission" in msg:
        return ProviderErrorCode.AUTH_FAILURE
    if "invalid" in msg or "400" in msg:
        return ProviderErrorCode.INVALID_INPUT
    if "timeout" in msg or "deadline" in msg:
        return ProviderErrorCode.TIMEOUT
    if "500" in msg or "unavailable" in msg or "internal" in msg:
        return ProviderErrorCode.SERVER_ERROR
    return ProviderErrorCode.UNKNOWN
