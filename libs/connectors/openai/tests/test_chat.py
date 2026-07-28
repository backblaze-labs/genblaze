"""Tests for genblaze_openai.chat (mocked — no real API calls)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_openai.chat import achat, chat


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


def test_multimodal_list_content_dumped_as_blocks(mock_client):
    """list[ContentBlock] content lands as a list of dicts with `type` discriminator."""
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef, TextContent

    msgs = [
        ChatMessage(
            role="user",
            content=[
                TextContent(text="What's in this image?"),
                ImageURLContent(image_url=ImageURLRef(url="https://x/y.png", detail="high")),
            ],
        )
    ]
    chat("gpt-4o", messages=msgs, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "What's in this image?"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png", "detail": "high"}},
    ]


def test_string_content_stays_string_on_wire(mock_client):
    """str content keeps the cheaper string wire shape — no auto-wrap to list."""
    msgs = [ChatMessage(role="user", content="hi")]
    chat("gpt-4o", messages=msgs, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["messages"][0]["content"] == "hi"  # str, not [{type: text, ...}]


def test_response_format_pydantic_class_wired(mock_client):
    """response_format=BaseModel auto-generates the json_schema envelope."""
    from pydantic import BaseModel

    class Summary(BaseModel):
        title: str

    chat("gpt-4o", prompt="x", response_format=Summary, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "Summary"


def test_response_format_dict_passthrough(mock_client):
    chat("gpt-4o", prompt="x", response_format={"type": "json_object"}, client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert payload["response_format"] == {"type": "json_object"}


def test_response_format_omitted_when_none(mock_client):
    chat("gpt-4o", prompt="x", client=mock_client)
    payload = mock_client.chat.completions.create.call_args[1]
    assert "response_format" not in payload


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


def test_internally_created_client_closed_once_across_retries(monkeypatch):
    """The client must be created once, reused across every retry attempt, and
    closed exactly once after the loop finishes — not per-attempt (#221)."""
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", lambda _s: None)
    fake_openai = MagicMock()
    created_clients: list[MagicMock] = []

    def _client_factory(**_kwargs):
        c = MagicMock()
        c.chat.completions.create.side_effect = [
            ProviderError(
                "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.1
            ),
            _mock_completion(),
        ]
        created_clients.append(c)
        return c

    fake_openai.OpenAI.side_effect = _client_factory
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    resp = chat("gpt-4o", prompt="hi", api_key="sk-test", retry_on_rate_limit=True)

    assert resp.text == "Hello!"
    assert fake_openai.OpenAI.call_count == 1  # one client for the whole retry loop
    assert created_clients[0].chat.completions.create.call_count == 2
    created_clients[0].close.assert_called_once()


def test_own_client_disables_sdk_retry_when_genblaze_manages_it(monkeypatch):
    """When genblaze creates its own client AND retry is genblaze-managed, the
    SDK's internal retry must be disabled (`max_retries=0`) — otherwise the SDK
    would retry underneath `call_with_rate_limit_retry` and `RetryPolicy.max_attempts`
    wouldn't be authoritative, multiplying rate-limited traffic (#221 panel review)."""
    from genblaze_core.providers.retry import RetryPolicy

    fake_openai = MagicMock()
    created_clients: list[MagicMock] = []

    def _client_factory(**_kwargs):
        c = MagicMock()
        c.chat.completions.create.side_effect = ProviderError(
            "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.1
        )
        created_clients.append(c)
        return c

    fake_openai.OpenAI.side_effect = _client_factory
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    with pytest.raises(ProviderError):
        chat("gpt-4o", prompt="hi", api_key="sk-test", retry_policy=RetryPolicy.disabled())

    assert fake_openai.OpenAI.call_args.kwargs["max_retries"] == 0
    # RetryPolicy.disabled() -> genblaze doesn't retry either -> exactly one SDK call.
    assert created_clients[0].chat.completions.create.call_count == 1


def test_own_client_keeps_default_sdk_retry_when_retry_not_opted_in(monkeypatch):
    """Default (opt-out) path must not touch `max_retries` — no behavior change
    for existing callers who rely on the SDK's own retry behavior."""
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value.chat.completions.create.return_value = _mock_completion()
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    chat("gpt-4o", prompt="hi", api_key="sk-test")

    assert "max_retries" not in fake_openai.OpenAI.call_args.kwargs


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


def test_cost_usd_always_none(mock_client):
    # cost_usd is always None — callers compute cost from tokens_in/out with their own rates.
    resp = chat("gpt-4o", prompt="hi", client=mock_client)
    assert resp.cost_usd is None


def test_achat_runs_in_thread(mock_client):
    import asyncio

    resp = asyncio.run(achat("gpt-4o", prompt="hi", client=mock_client))
    assert resp.text == "Hello!"


# --- Opt-in rate-limit backoff (#221) ---
#
# `chat()`/`achat()` already parse a 429's `Retry-After` hint onto
# `ProviderError.retry_after`; these tests cover the opt-in wait-and-retry
# loop built on top of that (`genblaze_core.providers.retry.call_with_rate_limit_retry`).
# `time.sleep` is patched at its source in `genblaze_core.providers.retry` so
# tests never actually block, and so we can assert the delay used.


def test_default_does_not_retry_on_rate_limit(mock_client, monkeypatch):
    """Opt-out is the default — an unadorned `chat()` call must still raise on the
    first 429, with no sleep and no second attempt."""
    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.chat.completions.create.side_effect = ProviderError(
        "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.5
    )

    with pytest.raises(ProviderError) as exc:
        chat("gpt-4o", prompt="hi", client=mock_client)

    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT
    assert mock_client.chat.completions.create.call_count == 1
    assert sleeps == []


def test_retry_on_rate_limit_waits_then_succeeds(mock_client, monkeypatch):
    """`retry_on_rate_limit=True` waits the server's `Retry-After` hint, then retries."""
    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.chat.completions.create.side_effect = [
        ProviderError("rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.5),
        _mock_completion(),
    ]

    resp = chat("gpt-4o", prompt="hi", client=mock_client, retry_on_rate_limit=True)

    assert resp.text == "Hello!"
    assert mock_client.chat.completions.create.call_count == 2
    assert sleeps == [0.5]  # server hint honored verbatim, not a computed backoff


def test_retry_on_rate_limit_raises_after_attempt_cap(mock_client, monkeypatch):
    """After `max_attempts`, the last rate-limit error still propagates."""
    from genblaze_core.providers.retry import RetryPolicy

    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", lambda _s: None)
    mock_client.chat.completions.create.side_effect = ProviderError(
        "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.1
    )

    with pytest.raises(ProviderError) as exc:
        chat(
            "gpt-4o",
            prompt="hi",
            client=mock_client,
            retry_on_rate_limit=True,
            retry_policy=RetryPolicy(max_attempts=3),
        )

    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT
    assert mock_client.chat.completions.create.call_count == 3


def test_retry_policy_alone_opts_in_without_the_flag(mock_client, monkeypatch):
    """Passing `retry_policy=` implies opt-in even without `retry_on_rate_limit=True`."""
    from genblaze_core.providers.retry import RetryPolicy

    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.chat.completions.create.side_effect = [
        ProviderError("rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.2),
        _mock_completion(),
    ]

    resp = chat(
        "gpt-4o", prompt="hi", client=mock_client, retry_policy=RetryPolicy(max_attempts=4)
    )

    assert resp.text == "Hello!"
    assert sleeps == [0.2]


def test_retry_on_rate_limit_does_not_retry_other_error_codes(mock_client, monkeypatch):
    """Only RATE_LIMIT is eligible — a content-policy error still fails fast even
    with retries enabled."""
    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.chat.completions.create.side_effect = ProviderError(
        "blocked", error_code=ProviderErrorCode.CONTENT_POLICY
    )

    with pytest.raises(ProviderError) as exc:
        chat("gpt-4o", prompt="hi", client=mock_client, retry_on_rate_limit=True)

    assert exc.value.error_code == ProviderErrorCode.CONTENT_POLICY
    assert mock_client.chat.completions.create.call_count == 1
    assert sleeps == []


def test_achat_retry_on_rate_limit(mock_client, monkeypatch):
    """`achat()` forwards retry kwargs through to `chat()` via `asyncio.to_thread`."""
    import asyncio

    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.chat.completions.create.side_effect = [
        ProviderError("rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.3),
        _mock_completion(),
    ]

    resp = asyncio.run(achat("gpt-4o", prompt="hi", client=mock_client, retry_on_rate_limit=True))

    assert resp.text == "Hello!"
    assert mock_client.chat.completions.create.call_count == 2
    assert sleeps == [0.3]
