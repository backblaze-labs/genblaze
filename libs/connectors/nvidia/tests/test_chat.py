"""Tests for NVIDIA chat (OpenAI-compatible path)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError

MISSING_OPENAI_MSG = 'openai package not installed. Run: pip install "genblaze-nvidia[chat]"'


def test_chat_requires_api_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    from genblaze_nvidia import chat

    with pytest.raises(ProviderError) as info:
        chat("meta/llama-3.3-70b-instruct", prompt="hi")
    from genblaze_core.models.enums import ProviderErrorCode

    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_chat_uses_injected_client():
    """Pre-built client short-circuits the ``import openai`` path."""
    from genblaze_nvidia import chat

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {
        "model": "meta/llama-3.3-70b-instruct",
        "choices": [{"message": {"content": "hello!"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    fake_client.chat.completions.create.return_value = fake_resp

    resp = chat(
        "meta/llama-3.3-70b-instruct",
        prompt="hi",
        client=fake_client,
    )
    assert resp.text == "hello!"
    assert resp.tokens_in == 5
    assert resp.tokens_out == 2
    # NIM has no public per-token pricing — cost is None by design.
    assert resp.cost_usd is None


def test_multimodal_blocks_dumped_to_nim_wire():
    """ImageURLContent + VideoURLContent dump to OpenAI-vision-shape dicts NIM accepts."""
    from genblaze_core.models.chat import (
        ChatMessage,
        ImageURLContent,
        ImageURLRef,
        TextContent,
        VideoURLContent,
        VideoURLRef,
    )
    from genblaze_nvidia import chat

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    fake_client.chat.completions.create.return_value = fake_resp

    msgs = [
        ChatMessage(
            role="user",
            content=[
                TextContent(text="describe these"),
                ImageURLContent(image_url=ImageURLRef(url="https://x/a.png")),
                VideoURLContent(video_url=VideoURLRef(url="https://x/b.mp4")),
            ],
        )
    ]
    chat("nvidia/nemotron-3-nano-omni", messages=msgs, client=fake_client)
    sent = fake_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert sent == [
        {"type": "text", "text": "describe these"},
        {"type": "image_url", "image_url": {"url": "https://x/a.png"}},
        {"type": "video_url", "video_url": {"url": "https://x/b.mp4"}},
    ]


def test_chat_forwards_system_and_temperature():
    from genblaze_nvidia import chat

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    fake_client.chat.completions.create.return_value = fake_resp

    chat(
        "nvidia/nemotron-4-340b-instruct",
        prompt="hi",
        system="be brief",
        temperature=0.7,
        max_tokens=42,
        client=fake_client,
    )
    call = fake_client.chat.completions.create.call_args.kwargs
    assert call["messages"][0] == {"role": "system", "content": "be brief"}
    assert call["temperature"] == 0.7
    assert call["max_tokens"] == 42


def test_chat_classifies_errors():
    """SDK exception → ProviderError with a classified error_code."""
    from genblaze_core.models.enums import ProviderErrorCode
    from genblaze_nvidia import chat

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = Exception("blocked by safety filter")

    with pytest.raises(ProviderError) as info:
        chat("meta/llama-3.3-70b-instruct", prompt="x", client=fake_client)
    assert info.value.error_code == ProviderErrorCode.CONTENT_POLICY


def test_chat_reads_nvidia_chat_base_url_env(monkeypatch):
    """Self-hosted NIM users set NVIDIA_CHAT_BASE_URL — chat() must honor it."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("NVIDIA_CHAT_BASE_URL", "https://self-hosted.example/v1")

    constructed = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructed.update(kwargs)
            self.chat = MagicMock()
            self.chat.completions = MagicMock()
            resp = MagicMock()
            resp.model_dump.return_value = {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }
            self.chat.completions.create.return_value = resp

        def close(self):
            pass

    import sys

    fake_mod = MagicMock()
    fake_mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_mod)

    from genblaze_nvidia import chat

    chat("meta/llama-3.3-70b-instruct", prompt="hi")
    assert constructed["base_url"] == "https://self-hosted.example/v1"


def test_response_format_pydantic_class_wired_to_nim():
    """NIM is OpenAI-wire-compat; response_format=BaseModel produces json_schema envelope."""
    from genblaze_nvidia import chat
    from pydantic import BaseModel

    class Summary(BaseModel):
        title: str

    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    fake_client.chat.completions.create.return_value = fake_resp

    chat("nvidia/nemotron-3-nano-omni", prompt="x", response_format=Summary, client=fake_client)
    payload = fake_client.chat.completions.create.call_args.kwargs
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "Summary"


def test_chat_explicit_base_url_overrides_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("NVIDIA_CHAT_BASE_URL", "https://from-env.example/v1")

    constructed = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructed.update(kwargs)
            self.chat = MagicMock()
            self.chat.completions = MagicMock()
            resp = MagicMock()
            resp.model_dump.return_value = {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }
            self.chat.completions.create.return_value = resp

        def close(self):
            pass

    import sys

    fake_mod = MagicMock()
    fake_mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_mod)

    from genblaze_nvidia import chat

    chat("meta/llama-3.3-70b-instruct", prompt="hi", base_url="https://explicit.example/v1")
    assert constructed["base_url"] == "https://explicit.example/v1"


def test_chat_raises_when_openai_missing(monkeypatch):
    """Without openai installed and no injected client, surfaces a clear error.

    `openai` is the `[chat]` extra, not a hard dep, so this is a real user
    state. Rather than skipping when openai happens to be installed (it always
    is in CI, which left this branch untested), evict it from sys.modules — a
    None entry there makes `import openai` raise ImportError regardless of what
    is on disk. Same idiom as the langsmith and s3 connectors use.
    """
    import sys

    from genblaze_nvidia import chat

    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setitem(sys.modules, "openai", None)

    with pytest.raises(ProviderError) as info:
        chat("meta/llama-3.3-70b-instruct", prompt="hi")
    # Equality, not `match=` — pytest's match is re.search, so it would pass on
    # a message that merely contains the hint alongside unwanted text.
    assert str(info.value) == MISSING_OPENAI_MSG
