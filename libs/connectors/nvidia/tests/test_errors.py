"""Tests for NVIDIA error mapping."""

from __future__ import annotations

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_nvidia._errors import map_nvidia_error


def test_401_is_auth_failure():
    assert map_nvidia_error(Exception("nope"), 401) == ProviderErrorCode.AUTH_FAILURE


def test_403_is_auth_failure():
    assert map_nvidia_error(Exception("nope"), 403) == ProviderErrorCode.AUTH_FAILURE


def test_404_is_model_error():
    assert map_nvidia_error(Exception("unknown model"), 404) == ProviderErrorCode.MODEL_ERROR


def test_429_is_rate_limit():
    assert map_nvidia_error(Exception("slow down"), 429) == ProviderErrorCode.RATE_LIMIT


def test_400_is_invalid_input():
    assert map_nvidia_error(Exception("bad req"), 400) == ProviderErrorCode.INVALID_INPUT


def test_500_is_server_error():
    assert map_nvidia_error(Exception("boom"), 502) == ProviderErrorCode.SERVER_ERROR


def test_content_policy_wins_over_400():
    """A 400 carrying a safety / policy marker must classify as CONTENT_POLICY,
    not INVALID_INPUT — otherwise callers burn retries on a deterministic refusal."""
    assert (
        map_nvidia_error(Exception("blocked by safety filter"), 400)
        == ProviderErrorCode.CONTENT_POLICY
    )
    assert (
        map_nvidia_error(Exception("content policy violation"), 400)
        == ProviderErrorCode.CONTENT_POLICY
    )
    assert (
        map_nvidia_error(Exception("nemoguard flagged this prompt"), 400)
        == ProviderErrorCode.CONTENT_POLICY
    )


def test_content_policy_without_status():
    """Guardrail markers in the message alone should still classify correctly."""
    assert map_nvidia_error(Exception("guardrail triggered")) == ProviderErrorCode.CONTENT_POLICY


def test_status_code_priority_over_message():
    """A 429 with an auth-adjacent message is still a rate-limit."""
    assert map_nvidia_error(Exception("unauthorized"), 429) == ProviderErrorCode.RATE_LIMIT


def test_falls_back_to_default_classifier():
    """No status + ambiguous message → delegates to classify_api_error."""
    assert map_nvidia_error(Exception("request timed out")) == ProviderErrorCode.TIMEOUT
