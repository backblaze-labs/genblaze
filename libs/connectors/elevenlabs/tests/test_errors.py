"""Tests for ElevenLabs error mapping."""

from __future__ import annotations

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_elevenlabs._errors import map_elevenlabs_error


class _ApiErrorLike(Exception):
    """Stand-in for elevenlabs.errors.* — real SDK exceptions carry
    ``status_code`` as an attribute (see elevenlabs.core.api_error.ApiError)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def test_rate_limit():
    assert map_elevenlabs_error(Exception("429 rate limit")) == ProviderErrorCode.RATE_LIMIT


def test_auth_failure():
    assert map_elevenlabs_error(Exception("401 unauthorized")) == ProviderErrorCode.AUTH_FAILURE


def test_invalid_input():
    assert (
        map_elevenlabs_error(Exception("400 invalid request")) == ProviderErrorCode.INVALID_INPUT
    )


def test_timeout():
    assert map_elevenlabs_error(Exception("request timed out")) == ProviderErrorCode.TIMEOUT


def test_server_error():
    assert map_elevenlabs_error(Exception("502 bad gateway")) == ProviderErrorCode.SERVER_ERROR


def test_unknown():
    assert map_elevenlabs_error(Exception("something went wrong")) == ProviderErrorCode.UNKNOWN


def test_404_status_code_is_model_error():
    """elevenlabs.errors.NotFoundError carries status_code=404 for an
    unknown model_id/voice_id — must map to MODEL_ERROR so
    `fallback_models` can fire (#167)."""
    assert (
        map_elevenlabs_error(_ApiErrorLike("not found", status_code=404))
        == ProviderErrorCode.MODEL_ERROR
    )


def test_model_not_found_message_without_status_code():
    """String-only fallback (delegated to classify_api_error) for callers
    that don't surface status_code."""
    assert (
        map_elevenlabs_error(Exception("model not found: no such model"))
        == ProviderErrorCode.MODEL_ERROR
    )


def test_status_code_priority_over_message():
    """status_code wins even when the message reads like something else."""
    assert (
        map_elevenlabs_error(_ApiErrorLike("rate limited", status_code=404))
        == ProviderErrorCode.MODEL_ERROR
    )
