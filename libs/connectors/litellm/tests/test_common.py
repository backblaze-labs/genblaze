"""Tests for the shared LiteLLM helpers and error mapping."""

from __future__ import annotations

import logging

import litellm
import pytest
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_litellm._common import credential_kwargs, import_litellm, response_cost
from genblaze_litellm._errors import map_litellm_error


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (
            litellm.ContentPolicyViolationError(message="x", model="m", llm_provider="p"),
            ProviderErrorCode.CONTENT_POLICY,
        ),
        (
            litellm.AuthenticationError(message="x", llm_provider="p", model="m"),
            ProviderErrorCode.AUTH_FAILURE,
        ),
        (
            litellm.RateLimitError(message="x", llm_provider="p", model="m"),
            ProviderErrorCode.RATE_LIMIT,
        ),
        (litellm.Timeout(message="x", model="m", llm_provider="p"), ProviderErrorCode.TIMEOUT),
        (
            litellm.NotFoundError(message="x", model="m", llm_provider="p"),
            ProviderErrorCode.MODEL_ERROR,
        ),
        (
            litellm.BadRequestError(message="x", model="m", llm_provider="p"),
            ProviderErrorCode.INVALID_INPUT,
        ),
        (
            litellm.ServiceUnavailableError(message="x", llm_provider="p", model="m"),
            ProviderErrorCode.SERVER_ERROR,
        ),
        (ValueError("rate limit hit"), ProviderErrorCode.RATE_LIMIT),
    ],
)
def test_map_litellm_error(exc, code):
    assert map_litellm_error(exc) == code


def test_credential_kwargs_omit_blank_values():
    assert credential_kwargs(None, "") == {}
    assert credential_kwargs("k", "https://b") == {"api_key": "k", "api_base": "https://b"}


def test_response_cost():
    class R:
        _hidden_params = {"response_cost": 0.5}

    assert response_cost(R()) == 0.5
    assert response_cost(object()) is None


def test_import_litellm_quiets_console_unless_debugging(monkeypatch):
    lite_logger = logging.getLogger("LiteLLM")
    monkeypatch.setattr(lite_logger, "level", logging.NOTSET)
    monkeypatch.setattr(litellm, "suppress_debug_info", False)

    monkeypatch.setenv("LITELLM_LOG", "DEBUG")
    import_litellm()
    assert litellm.suppress_debug_info is False
    assert lite_logger.level == logging.NOTSET

    monkeypatch.delenv("LITELLM_LOG")
    import_litellm()
    assert litellm.suppress_debug_info is True
    assert lite_logger.level == logging.WARNING
