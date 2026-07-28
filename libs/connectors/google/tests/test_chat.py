"""Tests for genblaze_google.chat (mocked — no real API calls)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_google.chat import achat, chat


def _mock_response(
    text: str = "Hello!",
    tokens_in: int = 10,
    tokens_out: int = 5,
    finish_reason: str = "STOP",
    function_calls: list[dict] | None = None,
    model_version: str = "gemini-2.5-flash-001",
):
    parts: list[dict] = []
    if text:
        parts.append({"text": text})
    for fc in function_calls or []:
        parts.append({"function_call": fc})
    payload = {
        "candidates": [
            {
                "finish_reason": finish_reason,
                "content": {"parts": parts, "role": "model"},
            }
        ],
        "usage_metadata": {
            "prompt_token_count": tokens_in,
            "candidates_token_count": tokens_out,
        },
        "model_version": model_version,
    }
    obj = MagicMock()
    obj.model_dump.return_value = payload
    return obj


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.models.generate_content.return_value = _mock_response()
    return client


def test_data_uri_image_translated_to_inline_data(mock_client):
    """A `data:` URI ImageURLContent block becomes a Gemini `inline_data` part.

    Regression test for #194: genblaze-google's chat() used to reject any
    ImageURLContent block pre-flight, breaking provider-swappable multimodal
    with the OpenAI-wire connectors (which accept it directly).
    """
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef

    data_uri = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    msgs = [
        ChatMessage(role="user", content=[ImageURLContent(image_url=ImageURLRef(url=data_uri))])
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    parts = mock_client.models.generate_content.call_args.kwargs["contents"][0]["parts"]
    assert parts == [{"inline_data": {"mime_type": "image/jpeg", "data": "/9j/4AAQSkZJRg=="}}]


def test_text_and_image_multimodal_message_round_trips(mock_client):
    """Exact repro from #194: TextContent + ImageURLContent(data URI) in one message."""
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef, TextContent

    data_uri = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    msgs = [
        ChatMessage(
            role="user",
            content=[
                TextContent(text="Describe this frame."),
                ImageURLContent(image_url=ImageURLRef(url=data_uri)),
            ],
        )
    ]
    resp = chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    parts = mock_client.models.generate_content.call_args.kwargs["contents"][0]["parts"]
    assert parts == [
        {"text": "Describe this frame."},
        {"inline_data": {"mime_type": "image/jpeg", "data": "/9j/4AAQSkZJRg=="}},
    ]
    assert resp.text == "Hello!"


def test_raw_dict_message_with_image_url_also_works(mock_client):
    """The raw-dict workaround the old error message recommended must actually work too —
    `_normalize_to_gemini` coerces dicts back through `ChatMessage(**m)`, so the fix for
    typed `ImageURLContent` has to cover this path as well (#194)."""
    data_uri = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    raw = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this frame."},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    }
    chat("gemini-2.5-flash", messages=[raw], client=mock_client)
    parts = mock_client.models.generate_content.call_args.kwargs["contents"][0]["parts"]
    assert parts == [
        {"text": "Describe this frame."},
        {"inline_data": {"mime_type": "image/jpeg", "data": "/9j/4AAQSkZJRg=="}},
    ]


def test_https_image_url_translated_to_file_data(mock_client):
    """A non-`data:` URL maps to Gemini's `file_data` part; mime type guessed from
    extension when `media_type` isn't given."""
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef

    msgs = [
        ChatMessage(
            role="user",
            content=[ImageURLContent(image_url=ImageURLRef(url="https://x/y.png"))],
        )
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    parts = mock_client.models.generate_content.call_args.kwargs["contents"][0]["parts"]
    assert parts == [{"file_data": {"mime_type": "image/png", "file_uri": "https://x/y.png"}}]


def test_data_uri_base64_marker_is_case_insensitive(mock_client):
    """RFC 2397 doesn't mandate lowercase `;base64,`; accept `;BASE64,` too."""
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef

    msgs = [
        ChatMessage(
            role="user",
            content=[
                ImageURLContent(
                    image_url=ImageURLRef(url="data:image/jpeg;BASE64,/9j/4AAQSkZJRg==")
                )
            ],
        )
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    parts = mock_client.models.generate_content.call_args.kwargs["contents"][0]["parts"]
    assert parts == [{"inline_data": {"mime_type": "image/jpeg", "data": "/9j/4AAQSkZJRg=="}}]


def test_data_uri_with_malformed_mime_header_falls_back_to_octet_stream(mock_client):
    """A data-URI header that doesn't look like `type/subtype` (e.g. injected control
    chars or stray whitespace) must never be forwarded verbatim as `mime_type` — fall
    back to a safe default instead."""
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef

    msgs = [
        ChatMessage(
            role="user",
            content=[
                ImageURLContent(
                    image_url=ImageURLRef(url="data:not a mime type;base64,/9j/4AAQSkZJRg==")
                )
            ],
        )
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    parts = mock_client.models.generate_content.call_args.kwargs["contents"][0]["parts"]
    assert parts == [
        {"inline_data": {"mime_type": "application/octet-stream", "data": "/9j/4AAQSkZJRg=="}}
    ]


def test_malformed_data_uri_raises_invalid_input(mock_client):
    """A `data:` URI missing the comma-delimited payload is a client error, not a Gemini one."""
    from genblaze_core.models.chat import ImageURLContent, ImageURLRef

    msgs = [
        ChatMessage(
            role="user",
            content=[ImageURLContent(image_url=ImageURLRef(url="data:image/jpeg;base64"))],
        )
    ]
    with pytest.raises(ProviderError) as info:
        chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_video_block_refused_with_clear_message(mock_client):
    from genblaze_core.models.chat import VideoURLContent, VideoURLRef

    msgs = [
        ChatMessage(
            role="user", content=[VideoURLContent(video_url=VideoURLRef(url="https://x/y.mp4"))]
        )
    ]
    with pytest.raises(ProviderError) as info:
        chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT
    assert "VideoURLContent" in str(info.value)


def test_text_only_list_content_translated(mock_client):
    """All-text list content translates cleanly to Gemini's parts shape — one
    `parts` entry per content block, matching the OpenAI connector's block-per-part
    translation (needed so image blocks can be interleaved, see #194)."""
    from genblaze_core.models.chat import TextContent

    msgs = [
        ChatMessage(role="user", content=[TextContent(text="hello"), TextContent(text=" world")])
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    call = mock_client.models.generate_content.call_args.kwargs
    parts = call["contents"][0]["parts"]
    assert parts == [{"text": "hello"}, {"text": " world"}]


def test_prompt_shorthand(mock_client):
    resp = chat("gemini-2.5-flash", prompt="hi", client=mock_client)
    payload = mock_client.models.generate_content.call_args[1]
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert resp.text == "Hello!"
    assert resp.tokens_in == 10
    assert resp.tokens_out == 5


def test_system_extracted_to_config(mock_client):
    chat("gemini-2.5-flash", prompt="hi", system="be terse", client=mock_client)
    payload = mock_client.models.generate_content.call_args[1]
    assert payload["config"]["system_instruction"] == "be terse"
    # System message should NOT be in `contents`
    assert all("system" not in p["role"] for p in payload["contents"])


def test_assistant_role_mapped_to_model(mock_client):
    msgs = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello!"),
        ChatMessage(role="user", content="bye"),
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    payload = mock_client.models.generate_content.call_args[1]
    roles = [c["role"] for c in payload["contents"]]
    assert roles == ["user", "model", "user"]


def test_system_message_pulled_into_system_instruction(mock_client):
    """A `system` role message in the list should land in system_instruction, not contents."""
    msgs = [
        ChatMessage(role="system", content="be polite"),
        ChatMessage(role="user", content="hi"),
    ]
    chat("gemini-2.5-flash", messages=msgs, client=mock_client)
    payload = mock_client.models.generate_content.call_args[1]
    assert payload["config"]["system_instruction"] == "be polite"
    assert len(payload["contents"]) == 1
    assert payload["contents"][0]["role"] == "user"


def test_requires_messages_or_prompt(mock_client):
    with pytest.raises(ProviderError) as exc:
        chat("gemini-2.5-flash", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_finish_reason_enum_stringified(mock_client):
    """Real SDK returns FinishReason enum; we must stringify for the str field."""

    class _FR:
        def __str__(self) -> str:
            return "FinishReason.STOP"

    mock_client.models.generate_content.return_value = _mock_response(finish_reason=_FR())
    resp = chat("gemini-2.5-flash", prompt="hi", client=mock_client)
    assert resp.finish_reason == "STOP"


def test_function_call_parsed(mock_client):
    mock_client.models.generate_content.return_value = _mock_response(
        text="",
        function_calls=[{"name": "get_weather", "args": {"city": "Tokyo"}}],
        finish_reason="STOP",
    )
    resp = chat("gemini-2.5-flash", prompt="weather?", client=mock_client)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Tokyo"}


def test_temperature_and_max_tokens(mock_client):
    chat(
        "gemini-2.5-flash",
        prompt="hi",
        temperature=0.5,
        max_tokens=100,
        client=mock_client,
    )
    payload = mock_client.models.generate_content.call_args[1]
    assert payload["config"]["temperature"] == 0.5
    assert payload["config"]["max_output_tokens"] == 100


def test_api_error_wrapped(mock_client):
    mock_client.models.generate_content.side_effect = Exception("RESOURCE_EXHAUSTED 429")
    with pytest.raises(ProviderError) as exc:
        chat("gemini-2.5-flash", prompt="hi", client=mock_client)
    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT


def test_cost_usd_always_none(mock_client):
    # cost_usd is always None — callers compute cost from tokens_in/out with their own rates.
    resp = chat("gemini-2.5-flash", prompt="hi", client=mock_client)
    assert resp.cost_usd is None


def test_achat_runs_in_thread(mock_client):
    resp = asyncio.run(achat("gemini-2.5-flash", prompt="hi", client=mock_client))
    assert resp.text == "Hello!"


def test_internally_created_client_closed_once_across_retries(monkeypatch):
    """The client must be created once, reused across every retry attempt, and
    closed exactly once after the loop finishes — not per-attempt (#221)."""
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", lambda _s: None)
    fake_google = MagicMock()
    created_clients: list[MagicMock] = []

    def _client_factory(**_kwargs):
        c = MagicMock()
        c.models.generate_content.side_effect = [
            ProviderError(
                "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.1
            ),
            _mock_response(),
        ]
        created_clients.append(c)
        return c

    fake_google.genai.Client.side_effect = _client_factory
    monkeypatch.setitem(__import__("sys").modules, "google", fake_google)

    resp = chat("gemini-2.5-flash", prompt="hi", api_key="test-key", retry_on_rate_limit=True)

    assert resp.text == "Hello!"
    assert fake_google.genai.Client.call_count == 1  # one client for the whole retry loop
    assert created_clients[0].models.generate_content.call_count == 2
    created_clients[0].close.assert_called_once()


def test_own_client_disables_sdk_retry_when_genblaze_manages_it(monkeypatch):
    """When genblaze creates its own client AND retry is genblaze-managed, the
    SDK's internal retry must be disabled (a single-attempt `HttpRetryOptions`)
    — otherwise the SDK would retry underneath `call_with_rate_limit_retry` and
    `RetryPolicy.max_attempts` wouldn't be authoritative, multiplying
    rate-limited traffic (#221 panel review)."""
    from genblaze_core.providers.retry import RetryPolicy

    fake_google = MagicMock()
    created_clients: list[MagicMock] = []

    def _client_factory(**_kwargs):
        c = MagicMock()
        c.models.generate_content.side_effect = ProviderError(
            "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.1
        )
        created_clients.append(c)
        return c

    fake_google.genai.Client.side_effect = _client_factory
    monkeypatch.setitem(__import__("sys").modules, "google", fake_google)

    with pytest.raises(ProviderError):
        chat(
            "gemini-2.5-flash",
            prompt="hi",
            api_key="test-key",
            retry_policy=RetryPolicy.disabled(),
        )

    assert "http_options" in fake_google.genai.Client.call_args.kwargs
    # RetryPolicy.disabled() -> genblaze doesn't retry either -> exactly one SDK call.
    assert created_clients[0].models.generate_content.call_count == 1


def test_own_client_keeps_default_sdk_retry_when_retry_not_opted_in(monkeypatch):
    """Default (opt-out) path must not set `http_options` — no behavior change
    for existing callers who rely on the SDK's own retry behavior."""
    fake_google = MagicMock()
    fake_google.genai.Client.return_value.models.generate_content.return_value = _mock_response()
    monkeypatch.setitem(__import__("sys").modules, "google", fake_google)

    chat("gemini-2.5-flash", prompt="hi", api_key="test-key")

    assert "http_options" not in fake_google.genai.Client.call_args.kwargs


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
    mock_client.models.generate_content.side_effect = ProviderError(
        "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.5
    )

    with pytest.raises(ProviderError) as exc:
        chat("gemini-2.5-flash", prompt="hi", client=mock_client)

    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT
    assert mock_client.models.generate_content.call_count == 1
    assert sleeps == []


def test_retry_on_rate_limit_waits_then_succeeds(mock_client, monkeypatch):
    """`retry_on_rate_limit=True` waits the server's `Retry-After` hint, then retries."""
    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.models.generate_content.side_effect = [
        ProviderError("rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.5),
        _mock_response(),
    ]

    resp = chat("gemini-2.5-flash", prompt="hi", client=mock_client, retry_on_rate_limit=True)

    assert resp.text == "Hello!"
    assert mock_client.models.generate_content.call_count == 2
    assert sleeps == [0.5]  # server hint honored verbatim, not a computed backoff


def test_retry_on_rate_limit_raises_after_attempt_cap(mock_client, monkeypatch):
    """After `max_attempts`, the last rate-limit error still propagates."""
    from genblaze_core.providers.retry import RetryPolicy

    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", lambda _s: None)
    mock_client.models.generate_content.side_effect = ProviderError(
        "rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.1
    )

    with pytest.raises(ProviderError) as exc:
        chat(
            "gemini-2.5-flash",
            prompt="hi",
            client=mock_client,
            retry_on_rate_limit=True,
            retry_policy=RetryPolicy(max_attempts=3),
        )

    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT
    assert mock_client.models.generate_content.call_count == 3


def test_retry_policy_alone_opts_in_without_the_flag(mock_client, monkeypatch):
    """Passing `retry_policy=` implies opt-in even without `retry_on_rate_limit=True`."""
    from genblaze_core.providers.retry import RetryPolicy

    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.models.generate_content.side_effect = [
        ProviderError("rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.2),
        _mock_response(),
    ]

    resp = chat(
        "gemini-2.5-flash",
        prompt="hi",
        client=mock_client,
        retry_policy=RetryPolicy(max_attempts=4),
    )

    assert resp.text == "Hello!"
    assert sleeps == [0.2]


def test_retry_on_rate_limit_does_not_retry_other_error_codes(mock_client, monkeypatch):
    """Only RATE_LIMIT is eligible — a content-policy error still fails fast even
    with retries enabled."""
    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.models.generate_content.side_effect = ProviderError(
        "blocked", error_code=ProviderErrorCode.CONTENT_POLICY
    )

    with pytest.raises(ProviderError) as exc:
        chat("gemini-2.5-flash", prompt="hi", client=mock_client, retry_on_rate_limit=True)

    assert exc.value.error_code == ProviderErrorCode.CONTENT_POLICY
    assert mock_client.models.generate_content.call_count == 1
    assert sleeps == []


def test_achat_retry_on_rate_limit(mock_client, monkeypatch):
    """`achat()` forwards retry kwargs through to `chat()` via `asyncio.to_thread`."""
    sleeps: list[float] = []
    monkeypatch.setattr("genblaze_core.providers.retry.time.sleep", sleeps.append)
    mock_client.models.generate_content.side_effect = [
        ProviderError("rate limited", error_code=ProviderErrorCode.RATE_LIMIT, retry_after=0.3),
        _mock_response(),
    ]

    resp = asyncio.run(
        achat("gemini-2.5-flash", prompt="hi", client=mock_client, retry_on_rate_limit=True)
    )

    assert resp.text == "Hello!"
    assert mock_client.models.generate_content.call_count == 2
    assert sleeps == [0.3]
