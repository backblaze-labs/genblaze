"""Tests for the shared chat models (ChatMessage, ToolCall, ChatResponse)."""

from __future__ import annotations

import pytest
from genblaze_core.models.chat import ChatMessage, ChatResponse, ToolCall
from pydantic import ValidationError


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
