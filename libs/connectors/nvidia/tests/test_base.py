"""Tests for the shared NVIDIA HTTP client / helpers."""

from __future__ import annotations

import base64

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_nvidia._base import (
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_GEN_BASE_URL,
    NvidiaClient,
    build_generation_path,
    decode_base64_payload,
    extract_asset_urls,
    extract_base64_assets,
    extract_error_detail,
    resolve_api_key,
    unwrap_error_body,
)


def test_api_key_priority(monkeypatch):
    """Explicit api_key overrides NVIDIA_API_KEY; env var overrides NVIDIA_NIM_API_KEY."""
    monkeypatch.setenv("NVIDIA_API_KEY", "from-primary")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "from-secondary")
    assert resolve_api_key("explicit") == "explicit"
    assert resolve_api_key(None) == "from-primary"

    monkeypatch.delenv("NVIDIA_API_KEY")
    assert resolve_api_key(None) == "from-secondary"

    monkeypatch.delenv("NVIDIA_NIM_API_KEY")
    assert resolve_api_key(None) is None


def test_build_generation_path_happy():
    assert build_generation_path("stabilityai/stable-diffusion-xl") == (
        "/genai/stabilityai/stable-diffusion-xl"
    )
    # Leading / trailing slashes stripped so callers can't accidentally
    # build ``/genai//foo`` (which NIM would 404).
    assert build_generation_path("/nvidia/cosmos-1.0/") == "/genai/nvidia/cosmos-1.0"


def test_build_generation_path_rejects_empty():
    with pytest.raises(ProviderError):
        build_generation_path("")


def test_decode_base64_rejects_garbage():
    with pytest.raises(ProviderError) as info:
        decode_base64_payload("not-valid-base64!!!")
    assert info.value.error_code == ProviderErrorCode.SERVER_ERROR


def test_decode_base64_accepts_unpadded():
    """NIM endpoints occasionally emit unpadded base64 — we pad before decoding."""
    # "hello" → "aGVsbG8=" padded, "aGVsbG8" unpadded.
    assert decode_base64_payload("aGVsbG8") == b"hello"


def test_decode_base64_accepts_url_safe_alphabet():
    """URL-safe base64 uses '-'/'_' — normalize before decode."""
    # Standard b64 of b"\xfb\xff" is "+/8=". URL-safe variant is "-_8=".
    assert decode_base64_payload("-_8") == b"\xfb\xff"


# --- extract_error_detail ---


def test_extract_error_detail_detail_key():
    assert extract_error_detail({"detail": "safety filter triggered"}) == (
        "safety filter triggered"
    )


def test_extract_error_detail_nested_message():
    assert extract_error_detail({"error": {"message": "upstream failed"}}) == "upstream failed"


def test_extract_error_detail_prefers_raw():
    """Non-JSON responses stash the raw body under ``_raw`` — unwrap that first."""
    raw = '{"detail": "inner message"}'
    assert extract_error_detail({"_raw": raw}) == "inner message"


def test_extract_error_detail_falls_back_to_json():
    """Unrecognized dict shapes return valid JSON, not ugly Python repr."""
    body = {"foo": "bar", "baz": 1}
    detail = extract_error_detail(body)
    # Must be valid JSON (double quotes), not Python repr (single quotes).
    assert '"foo"' in detail
    assert "'foo'" not in detail


def test_extract_error_detail_empty_body():
    assert extract_error_detail({}) == ""


def test_extract_base64_assets_artifacts_shape():
    body = {
        "artifacts": [
            {"base64": base64.b64encode(b"hello").decode(), "mime_type": "image/png"},
            {"base64": base64.b64encode(b"world").decode()},
        ]
    }
    out = extract_base64_assets(body)
    assert [b for b, _ in out] == [b"hello", b"world"]
    assert [m for _, m in out] == ["image/png", None]


def test_extract_base64_assets_data_shape():
    """OpenAI-style ``{"data": [{"b64_json": ...}]}`` also works."""
    body = {"data": [{"b64_json": base64.b64encode(b"payload").decode()}]}
    out = extract_base64_assets(body)
    assert [b for b, _ in out] == [b"payload"]


def test_extract_base64_assets_singleton_key():
    """Falls back to top-level ``image`` / ``video`` / ``audio``."""
    body = {"image": base64.b64encode(b"single").decode()}
    out = extract_base64_assets(body)
    assert out == [(b"single", None)]


def test_extract_base64_assets_empty_when_nothing():
    assert extract_base64_assets({}) == []


def test_extract_asset_urls_artifacts_with_url():
    body = {
        "artifacts": [
            {"url": "https://nvcf.example/video.mp4"},
            {"signed_url": "https://nvcf.example/thumb.png"},
        ]
    }
    assert extract_asset_urls(body) == [
        "https://nvcf.example/video.mp4",
        "https://nvcf.example/thumb.png",
    ]


def test_extract_asset_urls_flat_keys():
    body = {"video_url": "https://nvcf.example/gen.mp4"}
    assert extract_asset_urls(body) == ["https://nvcf.example/gen.mp4"]


def test_unwrap_error_body_detail():
    raw = '{"detail": "safety filter triggered"}'
    assert unwrap_error_body(raw) == "safety filter triggered"


def test_unwrap_error_body_nested_message():
    raw = '{"error": {"message": "upstream failed"}}'
    assert unwrap_error_body(raw) == "upstream failed"


def test_unwrap_error_body_passes_plain_text():
    assert unwrap_error_body("upstream gateway timeout") == "upstream gateway timeout"


def test_client_requires_key_when_no_injection():
    c = NvidiaClient(api_key=None)
    with pytest.raises(ProviderError) as info:
        c.http()
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_client_base_url_overrides(monkeypatch):
    """Env var overrides kick in when no constructor arg is set."""
    monkeypatch.setenv("NVIDIA_GEN_BASE_URL", "https://self-hosted.example/v1")
    monkeypatch.setenv("NVIDIA_CHAT_BASE_URL", "https://self-hosted-chat.example/v1")
    c = NvidiaClient(api_key="nvapi-test")
    assert c._gen_base_url == "https://self-hosted.example/v1"
    assert c.chat_base_url == "https://self-hosted-chat.example/v1"


def test_client_defaults(monkeypatch):
    """Without overrides we hit the documented public URLs."""
    monkeypatch.delenv("NVIDIA_GEN_BASE_URL", raising=False)
    monkeypatch.delenv("NVIDIA_CHAT_BASE_URL", raising=False)
    c = NvidiaClient(api_key="nvapi-test")
    assert c._gen_base_url == DEFAULT_GEN_BASE_URL
    assert c.chat_base_url == DEFAULT_CHAT_BASE_URL
