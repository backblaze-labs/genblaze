"""Tests for genblaze_gmicloud.chat (mocked — no real HTTP calls)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_gmicloud.chat import achat, chat


def _mock_body(
    text: str = "Hello!",
    tokens_in: int = 10,
    tokens_out: int = 5,
    finish_reason: str = "stop",
    tool_calls: list[dict] | None = None,
    model: str = "deepseek-ai/DeepSeek-V3",
) -> dict:
    message = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [{"finish_reason": finish_reason, "message": message}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def _mock_client(body: dict | None = None, status_code: int = 200, text: str = ""):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = body or _mock_body()
    client.post.return_value = resp
    return client


@pytest.fixture
def mock_client():
    return _mock_client()


def test_prompt_shorthand(mock_client):
    resp = chat("deepseek-ai/DeepSeek-V3", prompt="hi", client=mock_client)
    body = mock_client.post.call_args[1]["json"]
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert resp.text == "Hello!"
    assert resp.tokens_in == 10
    assert resp.tokens_out == 5


def test_system_prepended(mock_client):
    chat("deepseek-ai/DeepSeek-V3", prompt="hi", system="be terse", client=mock_client)
    body = mock_client.post.call_args[1]["json"]
    assert body["messages"][0] == {"role": "system", "content": "be terse"}


def test_messages_chat_message_objects(mock_client):
    msgs = [ChatMessage(role="user", content="hi")]
    chat("deepseek-ai/DeepSeek-V3", messages=msgs, client=mock_client)
    body = mock_client.post.call_args[1]["json"]
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_response_format_pydantic_class_wired(mock_client):
    from pydantic import BaseModel

    class Summary(BaseModel):
        title: str

    chat("deepseek-ai/DeepSeek-V3", prompt="x", response_format=Summary, client=mock_client)
    body = mock_client.post.call_args[1]["json"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "Summary"


def test_response_format_dict_passthrough(mock_client):
    chat(
        "deepseek-ai/DeepSeek-V3",
        prompt="x",
        response_format={"type": "json_object"},
        client=mock_client,
    )
    body = mock_client.post.call_args[1]["json"]
    assert body["response_format"] == {"type": "json_object"}


def test_requires_messages_or_prompt(mock_client):
    with pytest.raises(ProviderError) as exc:
        chat("deepseek-ai/DeepSeek-V3", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_outbound_tool_calls_use_json_arguments(mock_client):
    """Assistant tool_calls must serialize arguments as JSON, not Python repr."""
    import json

    from genblaze_core.models.chat import ToolCall

    msgs = [
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="get_weather", arguments={"city": "Tokyo"})],
        ),
    ]
    chat("deepseek-ai/DeepSeek-V3", messages=msgs, client=mock_client)
    body = mock_client.post.call_args[1]["json"]
    args_str = body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args_str) == {"city": "Tokyo"}


def test_tool_calls_parsed(mock_client):
    body = _mock_body(
        text="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'},
            }
        ],
        finish_reason="tool_calls",
    )
    mock_client.post.return_value.json.return_value = body
    resp = chat("deepseek-ai/DeepSeek-V3", prompt="weather?", client=mock_client)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Tokyo"}


def test_http_error_wrapped(mock_client):
    mock_client.post.return_value.status_code = 429
    mock_client.post.return_value.text = '{"error": "rate limited"}'
    with pytest.raises(ProviderError) as exc:
        chat("deepseek-ai/DeepSeek-V3", prompt="hi", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT


def test_http_error_unwraps_json_body(mock_client):
    mock_client.post.return_value.status_code = 400
    mock_client.post.return_value.text = '{"error": "bad model name"}'
    with pytest.raises(ProviderError) as exc:
        chat("foo", prompt="hi", client=mock_client)
    assert "bad model name" in str(exc.value)


def test_transport_exception_wrapped(mock_client):
    mock_client.post.side_effect = RuntimeError("connection refused")
    with pytest.raises(ProviderError) as exc:
        chat("deepseek-ai/DeepSeek-V3", prompt="hi", client=mock_client)
    assert "connection refused" in str(exc.value)


def test_cost_is_none(mock_client):
    """GMICloud connector intentionally returns cost_usd=None."""
    resp = chat("deepseek-ai/DeepSeek-V3", prompt="hi", client=mock_client)
    assert resp.cost_usd is None


def test_extra_kwargs_passed(mock_client):
    chat("deepseek-ai/DeepSeek-V3", prompt="hi", client=mock_client, top_p=0.9)
    body = mock_client.post.call_args[1]["json"]
    assert body["top_p"] == 0.9


def test_no_api_key_raises():
    """Missing GMI_API_KEY without an explicit client → AUTH_FAILURE."""
    import os

    saved = os.environ.pop("GMI_API_KEY", None)
    try:
        with pytest.raises(ProviderError) as exc:
            chat("deepseek-ai/DeepSeek-V3", prompt="hi")
        assert exc.value.error_code == ProviderErrorCode.AUTH_FAILURE
    finally:
        if saved is not None:
            os.environ["GMI_API_KEY"] = saved


def test_achat_runs_in_thread(mock_client):
    resp = asyncio.run(achat("deepseek-ai/DeepSeek-V3", prompt="hi", client=mock_client))
    assert resp.text == "Hello!"
