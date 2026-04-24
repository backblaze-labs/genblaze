"""Tests for genblaze_openai.chat (mocked — no real API calls)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_openai.chat import _calc_cost, _lookup_rate, achat, chat


def _mock_completion(
    text: str = "Hello!",
    tokens_in: int = 10,
    tokens_out: int = 5,
    finish_reason: str = "stop",
    tool_calls: list[dict] | None = None,
    model: str = "gpt-4o",
):
    """Build a SimpleNamespace shaped like an OpenAI ChatCompletion."""
    message = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    payload = {
        "model": model,
        "choices": [{"finish_reason": finish_reason, "message": message}],
        "usage": {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    obj = MagicMock()
    obj.model_dump.return_value = payload
    return obj


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_completion()
    return client


def test_prompt_shorthand(mock_client):
    resp = chat("gpt-4o", prompt="hi", client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert resp.text == "Hello!"
    assert resp.tokens_in == 10
    assert resp.tokens_out == 5
    assert resp.finish_reason == "stop"


def test_system_prepended(mock_client):
    chat("gpt-4o", prompt="hi", system="be terse", client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["messages"][0] == {"role": "system", "content": "be terse"}
    assert payload["messages"][1]["role"] == "user"


def test_messages_chat_message_objects(mock_client):
    msgs = [
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
    ]
    chat("gpt-4o", messages=msgs, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["content"] == "hi"


def test_messages_dicts_passthrough(mock_client):
    chat(
        "gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        client=mock_client,
    )
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_requires_messages_or_prompt(mock_client):
    with pytest.raises(ProviderError) as exc:
        chat("gpt-4o", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_outbound_tool_calls_use_json_arguments(mock_client):
    """Assistant tool_calls in messages must serialize arguments as JSON, not Python repr."""
    import json

    from genblaze_core.models.chat import ToolCall

    msgs = [
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="get_weather", arguments={"city": "Tokyo"})],
        ),
        ChatMessage(role="tool", tool_call_id="c1", name="get_weather", content="72F"),
    ]
    chat("gpt-4o", messages=msgs, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    args_str = payload["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args_str) == {"city": "Tokyo"}


def test_tool_calls_parsed(mock_client):
    mock_client.chat.completions.create.return_value = _mock_completion(
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
    resp = chat("gpt-4o", prompt="weather?", client=mock_client)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Tokyo"}


def test_temperature_and_max_tokens_passed(mock_client):
    chat("gpt-4o", prompt="hi", temperature=0.7, max_tokens=100, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 100


def test_tools_passed(mock_client):
    tools = [{"type": "function", "function": {"name": "fn"}}]
    chat("gpt-4o", prompt="hi", tools=tools, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["tools"] == tools


def test_extra_kwargs_passed(mock_client):
    chat("gpt-4o", prompt="hi", client=mock_client, top_p=0.9)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["top_p"] == 0.9


def test_content_policy_classified(mock_client):
    """A content-policy refusal must map to CONTENT_POLICY, not INVALID_INPUT."""
    mock_client.chat.completions.create.side_effect = Exception(
        "400 content_policy_violation: your prompt was rejected by safety"
    )
    with pytest.raises(ProviderError) as exc:
        chat("gpt-4o", prompt="hi", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.CONTENT_POLICY


def test_external_client_not_closed(mock_client):
    """Caller-supplied clients outlive chat() calls."""
    chat("gpt-4o", prompt="hi", client=mock_client)
    mock_client.close.assert_not_called()


def test_internally_created_client_is_closed(monkeypatch):
    """When we create the client ourselves, we must close it to avoid transport leaks."""
    fake_openai = MagicMock()
    created_clients: list[MagicMock] = []

    def _client_factory(**_kwargs):
        c = MagicMock()
        c.chat.completions.create.return_value = _mock_completion()
        created_clients.append(c)
        return c

    fake_openai.OpenAI.side_effect = _client_factory
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    chat("gpt-4o", prompt="hi", api_key="sk-test")

    assert len(created_clients) == 1
    created_clients[0].close.assert_called_once()


def test_base_url_forwarded_to_sdk(monkeypatch):
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value.chat.completions.create.return_value = _mock_completion()
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    chat("gpt-4o", prompt="hi", api_key="sk-test", base_url="https://proxy.example/v1")

    fake_openai.OpenAI.assert_called_once()
    assert fake_openai.OpenAI.call_args.kwargs["base_url"] == "https://proxy.example/v1"


def test_api_error_wrapped(mock_client):
    mock_client.chat.completions.create.side_effect = Exception("rate limit exceeded 429")
    with pytest.raises(ProviderError) as exc:
        chat("gpt-4o", prompt="hi", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT


def test_cost_computed_for_known_model(mock_client):
    resp = chat("gpt-4o", prompt="hi", client=mock_client)
    # 10 in * 2.50/1M + 5 out * 10.00/1M = 0.000025 + 0.00005 = 7.5e-5
    assert resp.cost_usd is not None
    assert abs(resp.cost_usd - 7.5e-5) < 1e-9


def test_cost_none_for_unknown_model(mock_client):
    mock_client.chat.completions.create.return_value = _mock_completion(model="foo-bar")
    resp = chat("foo-bar", prompt="hi", client=mock_client)
    assert resp.cost_usd is None


def test_lookup_rate_strips_dated_suffix():
    assert _lookup_rate("gpt-4o-2024-11-20") == _lookup_rate("gpt-4o")
    assert _lookup_rate("gpt-4o-mini-2024-07-18") == _lookup_rate("gpt-4o-mini")


def test_calc_cost_returns_none_when_tokens_missing():
    assert _calc_cost("gpt-4o", None, 5) is None
    assert _calc_cost("gpt-4o", 5, None) is None


def test_achat_runs_in_thread(mock_client):
    import asyncio

    resp = asyncio.run(achat("gpt-4o", prompt="hi", client=mock_client))
    assert resp.text == "Hello!"
