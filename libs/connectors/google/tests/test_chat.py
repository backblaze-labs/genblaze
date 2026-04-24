"""Tests for genblaze_google.chat (mocked — no real API calls)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_google.chat import _calc_cost, _lookup_rate, achat, chat


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


def test_cost_for_known_model(mock_client):
    resp = chat("gemini-2.5-flash", prompt="hi", client=mock_client)
    # 10 in * 0.30/1M + 5 out * 2.50/1M
    expected = (10 / 1_000_000) * 0.30 + (5 / 1_000_000) * 2.50
    assert resp.cost_usd is not None
    assert abs(resp.cost_usd - expected) < 1e-9


def test_lookup_rate_strips_version_suffix():
    assert _lookup_rate("gemini-2.5-flash-001") == _lookup_rate("gemini-2.5-flash")
    assert _lookup_rate("gemini-2.5-flash-latest") == _lookup_rate("gemini-2.5-flash")


def test_calc_cost_returns_none_when_tokens_missing():
    assert _calc_cost("gemini-2.5-flash", None, 5) is None


def test_achat_runs_in_thread(mock_client):
    resp = asyncio.run(achat("gemini-2.5-flash", prompt="hi", client=mock_client))
    assert resp.text == "Hello!"
