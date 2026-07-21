"""Shared ElevenLabs error mapping — used by provider.py, sfx.py."""

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_elevenlabs_error(exc: Exception) -> ProviderErrorCode:
    """Map an ElevenLabs API exception to a ProviderErrorCode."""
    # 404 is the one status we dispatch on the attribute rather than the
    # message: NotFoundError (an unknown model_id or voice_id) must map to
    # MODEL_ERROR so the pipeline's `fallback_models` retry can fire (#167),
    # but the shared classifier only maps 404 → MODEL_ERROR when the body
    # contains "model", which ElevenLabs' 404 body does not guarantee. A bad
    # voice_id takes this path too — same tradeoff genblaze_nvidia accepts.
    # The other codes (401/403/429/5xx) are matched below via str(exc), which
    # embeds `status_code: NNN`; the local checks intentionally run before the
    # delegated classifier so this connector's historical mapping wins.
    if getattr(exc, "status_code", None) == 404:
        return ProviderErrorCode.MODEL_ERROR
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
    # Delegate remaining classification (including "model" + "not found"/
    # "not available" → MODEL_ERROR) to the shared classifier instead of
    # re-duplicating that pattern here.
    return classify_api_error(exc)
