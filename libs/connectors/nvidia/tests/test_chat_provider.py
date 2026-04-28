"""Tests for NvidiaChatProvider — NIM chat as a Pipeline step."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_nvidia.chat_provider import NvidiaChatProvider


def _fake_response(text: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.model_dump.return_value = {
        "model": "nvidia/nemotron-3-nano-omni",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return resp


def _fake_client(text: str = "ok") -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response(text)
    return client


def _step(prompt: str = "hi", inputs: list[Asset] | None = None) -> Step:
    return Step(
        provider="nvidia-chat",
        model="nvidia/nemotron-3-nano-omni",
        modality=Modality.TEXT,
        prompt=prompt,
        inputs=inputs or [],
    )


def test_capabilities_advertises_image_video_inputs():
    p = NvidiaChatProvider(api_key="nvapi-test")
    caps = p.get_capabilities()
    assert Modality.TEXT in (caps.supported_modalities or [])
    assert "image" in (caps.supported_inputs or [])
    assert "video" in (caps.supported_inputs or [])
    assert caps.accepts_chain_input is True


def test_text_only_step_uses_string_content_wire():
    """Plain prompt → str content (cheaper wire shape, no auto-list-wrap)."""
    client = _fake_client("hello")
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    out = p.generate(_step("describe this"))
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["messages"][0]["content"] == "describe this"
    assert out.assets[0].metadata["text"] == "hello"
    assert out.cost_usd is None
    assert out.provider_payload["usage"] == {
        "tokens_in": 11,
        "tokens_out": 7,
        "tokens_cached": None,
    }


def test_image_input_built_as_image_url_block():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    asset = Asset(url="https://x/cat.png", media_type="image/png")
    p.generate(_step(prompt="what is this?", inputs=[asset]))
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert sent == [
        {"type": "text", "text": "what is this?"},
        {
            "type": "image_url",
            "image_url": {"url": "https://x/cat.png", "media_type": "image/png"},
        },
    ]


def test_video_input_built_as_video_url_block():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    asset = Asset(url="https://x/clip.mp4", media_type="video/mp4")
    p.generate(_step(prompt="summarize", inputs=[asset]))
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert sent[1] == {
        "type": "video_url",
        "video_url": {"url": "https://x/clip.mp4", "media_type": "video/mp4"},
    }


def test_audio_input_built_as_audio_url_block():
    """Nemotron 3 Nano Omni accepts audio_url blocks — verified upstream."""
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    asset = Asset(url="https://x/clip.wav", media_type="audio/wav")
    p.generate(_step(prompt="transcribe this", inputs=[asset]))
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert sent[1] == {
        "type": "audio_url",
        "audio_url": {"url": "https://x/clip.wav", "media_type": "audio/wav"},
    }


def test_audio_input_accepts_data_uri():
    """data:audio/wav;base64,... URIs are passed through (NIM accepts inline base64)."""
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    asset = Asset(url="data:audio/wav;base64,UklGRg==", media_type="audio/wav")
    p.generate(_step(prompt="transcribe", inputs=[asset]))
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert sent[1]["audio_url"]["url"] == "data:audio/wav;base64,UklGRg=="


def test_mixed_image_audio_video_inputs_all_translated():
    """Eyes-and-ears: a single step can carry all three modalities together."""
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    inputs = [
        Asset(url="https://x/a.png", media_type="image/png"),
        Asset(url="https://x/b.wav", media_type="audio/wav"),
        Asset(url="https://x/c.mp4", media_type="video/mp4"),
    ]
    p.generate(_step(prompt="describe", inputs=inputs))
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert [block["type"] for block in sent] == ["text", "image_url", "audio_url", "video_url"]


def test_pdf_input_raises_with_rasterization_hint():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    asset = Asset(url="https://x/doc.pdf", media_type="application/pdf")
    with pytest.raises(ProviderError) as info:
        p.generate(_step(prompt="summarize", inputs=[asset]))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT
    assert "rasterize" in str(info.value).lower()


def test_unsupported_media_type_raises():
    """Truly unsupported types (e.g. application/zip) error early with a clear message."""
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    asset = Asset(url="https://x/bundle.zip", media_type="application/zip")
    with pytest.raises(ProviderError) as info:
        p.generate(_step(prompt="x", inputs=[asset]))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT
    assert "image/*" in str(info.value)


def test_reasoning_none_omits_kwarg():
    """Default tri-state: do not send enable_thinking — let server pick per checkpoint."""
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client, reasoning=None)
    p.generate(_step("hi"))
    payload = client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in payload


def test_reasoning_false_sets_enable_thinking_false():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client, reasoning=False)
    p.generate(_step("hi"))
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_reasoning_true_sets_enable_thinking_true():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client, reasoning=True)
    p.generate(_step("hi"))
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_media_io_kwargs_threaded_into_extra_body():
    client = _fake_client()
    p = NvidiaChatProvider(
        api_key="nvapi-test",
        client=client,
        media_io_kwargs={"video": {"fps": 3.0}},
    )
    p.generate(_step("hi"))
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["extra_body"]["media_io_kwargs"] == {"video": {"fps": 3.0}}


def test_mm_processor_kwargs_threaded_into_extra_body():
    client = _fake_client()
    p = NvidiaChatProvider(
        api_key="nvapi-test",
        client=client,
        mm_processor_kwargs={"max_num_tiles": 3},
    )
    p.generate(_step("hi"))
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["extra_body"]["mm_processor_kwargs"] == {"max_num_tiles": 3}


def test_response_format_pydantic_class_threaded():
    """step.params['response_format'] gets coerced to the json_schema envelope."""
    from pydantic import BaseModel

    class Summary(BaseModel):
        title: str

    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    step = _step("summarize")
    step.params = {"response_format": Summary}
    p.generate(step)
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "Summary"


def test_step_params_temperature_passes_through():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    step = _step("hi")
    step.params = {"temperature": 0.2, "max_tokens": 64}
    p.generate(step)
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 64


def test_empty_prompt_and_no_inputs_raises():
    client = _fake_client()
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    step = Step(
        provider="nvidia-chat",
        model="nvidia/nemotron-3-nano-omni",
        modality=Modality.TEXT,
        prompt="",
        inputs=[],
    )
    with pytest.raises(ProviderError) as info:
        p.generate(step)
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_output_asset_has_stable_sha256_for_caching():
    """Same response text → same Asset.sha256, regardless of run."""
    client = _fake_client("deterministic answer")
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    out1 = p.generate(_step("q"))
    out2 = p.generate(_step("q"))
    assert out1.assets[0].sha256 == out2.assets[0].sha256
    assert out1.assets[0].sha256 is not None


def test_sdk_exception_mapped_to_provider_error():
    client = MagicMock()
    client.chat.completions.create.side_effect = Exception("blocked by safety filter")
    p = NvidiaChatProvider(api_key="nvapi-test", client=client)
    with pytest.raises(ProviderError) as info:
        p.generate(_step("x"))
    assert info.value.error_code == ProviderErrorCode.CONTENT_POLICY


def test_no_api_key_raises_auth_failure(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    p = NvidiaChatProvider()
    with pytest.raises(ProviderError) as info:
        p.generate(_step("x"))
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE
