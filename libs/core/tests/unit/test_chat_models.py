"""Tests for the shared chat models (ChatMessage, ToolCall, ChatResponse)."""

from __future__ import annotations

import pytest
from genblaze_core.models.chat import (
    AudioURLContent,
    AudioURLRef,
    ChatMessage,
    ChatResponse,
    ImageURLContent,
    ImageURLRef,
    TextContent,
    ToolCall,
    VideoURLContent,
    VideoURLRef,
    coerce_response_format,
)
from pydantic import BaseModel, ValidationError


def test_chat_message_minimal():
    msg = ChatMessage(role="user", content="hi")
    assert msg.role == "user"
    assert msg.content == "hi"
    assert msg.tool_calls is None


def test_chat_message_assistant_with_tool_calls():
    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="call_1", name="get_weather", arguments={"city": "Tokyo"})],
    )
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].name == "get_weather"


def test_chat_message_tool_role():
    msg = ChatMessage(role="tool", content="72F", tool_call_id="call_1", name="get_weather")
    assert msg.role == "tool"
    assert msg.tool_call_id == "call_1"


def test_chat_message_invalid_role():
    with pytest.raises(ValidationError):
        ChatMessage(role="not-a-role", content="hi")  # type: ignore[arg-type]


def test_tool_call_arguments_dict():
    tc = ToolCall(id="x", name="fn", arguments={"a": 1})
    assert tc.arguments == {"a": 1}


def test_tool_call_arguments_json_string_coerced():
    """OpenAI returns arguments as a JSON string — validator should parse to dict."""
    tc = ToolCall(id="x", name="fn", arguments='{"a": 1}')  # type: ignore[arg-type]
    assert tc.arguments == {"a": 1}


def test_tool_call_arguments_invalid_json_preserved_in_raw():
    tc = ToolCall(id="x", name="fn", arguments="not json")  # type: ignore[arg-type]
    assert tc.arguments == {"_raw": "not json"}


def test_chat_response_repr_truncates_long_text():
    resp = ChatResponse(text="a" * 200, model="gpt-4o", tokens_in=10, tokens_out=200)
    r = repr(resp)
    assert "gpt-4o" in r
    assert "…" in r
    assert "tokens=10/200" in r


def test_chat_response_defaults():
    resp = ChatResponse(model="gpt-4o")
    assert resp.text == ""
    assert resp.tool_calls == []
    assert resp.cost_usd is None
    assert resp.raw == {}


def test_chat_response_round_trip():
    resp = ChatResponse(
        text="hi",
        model="gpt-4o",
        tokens_in=5,
        tokens_out=2,
        tool_calls=[ToolCall(id="c1", name="fn", arguments={"x": 1})],
    )
    dumped = resp.model_dump()
    restored = ChatResponse(**dumped)
    assert restored.text == resp.text
    assert restored.tool_calls[0].name == "fn"


# Typed multimodal content blocks


def test_string_content_round_trips_unchanged():
    """The 95% case: passing a str keeps a str — no auto-wrap surprise."""
    msg = ChatMessage(role="user", content="hello")
    assert msg.content == "hello"
    assert isinstance(msg.content, str)


def test_list_content_validates_with_discriminator():
    """Plain dicts get dispatched to the right block type via the `type` discriminator."""
    msg = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ],
    )
    assert isinstance(msg.content, list)
    assert isinstance(msg.content[0], TextContent)
    assert isinstance(msg.content[1], ImageURLContent)
    assert msg.content[1].image_url.url == "https://example.com/cat.png"


def test_list_content_accepts_block_instances():
    msg = ChatMessage(
        role="user",
        content=[
            TextContent(text="describe this"),
            ImageURLContent(image_url=ImageURLRef(url="https://x/y.png", detail="high")),
            VideoURLContent(video_url=VideoURLRef(url="https://x/y.mp4")),
            AudioURLContent(audio_url=AudioURLRef(url="https://x/y.wav")),
        ],
    )
    assert len(msg.content) == 4
    assert msg.content[1].image_url.detail == "high"
    assert msg.content[3].audio_url.url == "https://x/y.wav"


def test_audio_block_round_trip():
    """AudioURLContent dumps to the NIM-verified `audio_url` wire shape."""
    block = AudioURLContent(audio_url=AudioURLRef(url="file:///tmp/a.wav", media_type="audio/wav"))
    dumped = block.model_dump(exclude_none=True)
    assert dumped == {
        "type": "audio_url",
        "audio_url": {"url": "file:///tmp/a.wav", "media_type": "audio/wav"},
    }


def test_unknown_block_type_rejected():
    """Unknown discriminator value (not in the union) still rejected."""
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content=[{"type": "input_audio", "input_audio": {"data": "x"}}])


def test_content_blocks_property_materializes_string():
    """Property gives a unified shape without mutating storage."""
    msg = ChatMessage(role="user", content="hi")
    blocks = msg.content_blocks
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == "hi"
    # storage stayed as str
    assert isinstance(msg.content, str)


def test_content_blocks_property_passes_through_list():
    img = ImageURLContent(image_url=ImageURLRef(url="https://x/y.png"))
    msg = ChatMessage(role="user", content=[TextContent(text="see"), img])
    blocks = msg.content_blocks
    assert blocks[1] is img


def test_content_blocks_property_empty_string_yields_empty_list():
    msg = ChatMessage(role="assistant", content="")
    assert msg.content_blocks == []


def test_block_dump_strips_none_fields():
    """exclude_none on the wire keeps payloads tight (no `detail: null` noise)."""
    block = ImageURLContent(image_url=ImageURLRef(url="https://x/y.png"))
    dumped = block.model_dump(exclude_none=True)
    assert dumped == {"type": "image_url", "image_url": {"url": "https://x/y.png"}}


def test_chat_message_list_content_round_trip():
    """list-shaped content survives model_dump → re-validate."""
    msg = ChatMessage(
        role="user",
        content=[
            TextContent(text="what's this?"),
            ImageURLContent(image_url=ImageURLRef(url="https://x/y.png")),
        ],
    )
    dumped = msg.model_dump()
    restored = ChatMessage(**dumped)
    assert isinstance(restored.content, list)
    assert isinstance(restored.content[1], ImageURLContent)


# response_format coercion


class _Summary(BaseModel):
    title: str
    score: int


def test_coerce_response_format_pydantic_class_to_json_schema():
    """A BaseModel subclass produces an OpenAI-wire json_schema envelope."""
    out = coerce_response_format(_Summary)
    assert out["type"] == "json_schema"
    assert out["json_schema"]["name"] == "_Summary"
    assert out["json_schema"]["strict"] is True
    schema = out["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert "title" in schema["properties"]


def test_coerce_response_format_dict_passthrough():
    raw = {"type": "json_object"}
    assert coerce_response_format(raw) is raw


def test_coerce_response_format_rejects_unsupported():
    with pytest.raises(TypeError):
        coerce_response_format("not allowed")  # type: ignore[arg-type]


def test_coerce_response_format_rejects_non_basemodel_class():
    class Plain:
        pass

    with pytest.raises(TypeError):
        coerce_response_format(Plain)  # type: ignore[arg-type]
