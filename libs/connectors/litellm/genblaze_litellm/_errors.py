"""Error mapping for the LiteLLM connector."""

from __future__ import annotations

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error

# LiteLLM maps every provider's failures onto one exception hierarchy, so the
# class name is a stronger signal than the message text. Matched by name
# (walking the MRO) so this module never imports litellm.
_EXCEPTION_CODES: dict[str, ProviderErrorCode] = {
    "ContentPolicyViolationError": ProviderErrorCode.CONTENT_POLICY,
    "AuthenticationError": ProviderErrorCode.AUTH_FAILURE,
    "PermissionDeniedError": ProviderErrorCode.AUTH_FAILURE,
    "RateLimitError": ProviderErrorCode.RATE_LIMIT,
    "Timeout": ProviderErrorCode.TIMEOUT,
    "NotFoundError": ProviderErrorCode.MODEL_ERROR,
    "UnsupportedParamsError": ProviderErrorCode.INVALID_INPUT,
    "ContextWindowExceededError": ProviderErrorCode.INVALID_INPUT,
    "BadRequestError": ProviderErrorCode.INVALID_INPUT,
    "UnprocessableEntityError": ProviderErrorCode.INVALID_INPUT,
    "InternalServerError": ProviderErrorCode.SERVER_ERROR,
    "ServiceUnavailableError": ProviderErrorCode.SERVER_ERROR,
    "BadGatewayError": ProviderErrorCode.SERVER_ERROR,
    "APIConnectionError": ProviderErrorCode.SERVER_ERROR,
}


def map_litellm_error(exc: Exception) -> ProviderErrorCode:
    """Map a LiteLLM exception to a ProviderErrorCode."""
    for cls in type(exc).__mro__:
        code = _EXCEPTION_CODES.get(cls.__name__)
        if code is not None:
            return code
    return classify_api_error(exc)
