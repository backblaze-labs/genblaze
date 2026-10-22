"""Tests for the LiteLLM chat() / achat() helpers (mocked)."""

from __future__ import annotations

import asyncio

import litellm
import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ToolCall
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.retry import RetryPolicy
from genblaze_litellm import achat, chat
from pydantic import BaseModel


def test_prompt_and_system_build_messages(fake_litellm):
    resp = chat("anthropic/claude-sonnet-4-6", prompt="One line about the sea.", system="Be brief")

    call = fake_litellm.kwargs_of("completion")[0]
    assert call["messages"] == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "One line about the sea."},
    ]
    assert call["drop_params"] is True
    assert "api_key" not in call
    assert resp.text == "Waves fold into light."
    assert resp.finish_reason == "stop"
    assert (resp.tokens_in, resp.tokens_out) == (21, 7)
    assert resp.cost_usd == pytest.approx(0.000168)


def test_tool_call_history_is_serialized(fake_litellm):
    messages = [
        ChatMessage(role="user", content="weather?"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="t1", name="get_weather", arguments={"city": "Oslo"})],
        ),
        ChatMessage(role="tool", content="4C", tool_call_id="t1"),
    ]
    chat(
        "bedrock/us.anthropic.claude-sonnet-4-6-v1:0", messages, api_key="k", api_base="https://b"
    )

    call = fake_litellm.kwargs_of("completion")[0]
    assert call["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"city": "Oslo"}'
    assert call["messages"][2]["tool_call_id"] == "t1"
    assert call["api_key"] == "k"
    assert call["api_base"] == "https://b"


def test_response_format_model_is_coerced(fake_litellm):
    class Line(BaseModel):
        text: str

    chat("openai/gpt-4.1-mini", prompt="x", response_format=Line, temperature=0.2, max_tokens=50)
    call = fake_litellm.kwargs_of("completion")[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["temperature"] == 0.2
    assert call["max_tokens"] == 50


def test_requires_messages_or_prompt(fake_litellm):
    with pytest.raises(ProviderError) as exc:
        chat("openai/gpt-4.1-mini")
    assert exc.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_rate_limit_retry_disables_sdk_retries(fake_litellm, monkeypatch):
    attempts = []

    def flaky(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise litellm.RateLimitError(message="slow down", llm_provider="groq", model="m")
        return fake_litellm.completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", flaky)
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", lambda _s: None)
    resp = chat("groq/llama-3.3-70b", prompt="x", retry_policy=RetryPolicy(max_attempts=3))
    assert resp.text == "Waves fold into light."
    assert len(attempts) == 2
    assert attempts[0]["max_retries"] == 0


def test_error_is_classified(fake_litellm):
    fake_litellm.raise_on["completion"] = litellm.ContentPolicyViolationError(
        message="blocked", model="m", llm_provider="openai"
    )
    with pytest.raises(ProviderError) as exc:
        chat("openai/gpt-4.1-mini", prompt="x")
    assert exc.value.error_code == ProviderErrorCode.CONTENT_POLICY


def test_achat(fake_litellm):
    resp = asyncio.run(achat("mistral/mistral-large-latest", prompt="hi"))
    assert resp.text == "Waves fold into light."
