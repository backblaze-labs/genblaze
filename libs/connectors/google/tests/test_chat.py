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
