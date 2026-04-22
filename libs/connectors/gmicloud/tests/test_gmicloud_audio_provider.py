"""Tests for GMICloudAudioProvider (mocked — no real API calls)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SubmitResult
from genblaze_core.testing import ProviderComplianceTests

from .conftest import make_mock_http_client


@pytest.fixture
def provider():
    from genblaze_gmicloud.audio import GMICloudAudioProvider

    p = GMICloudAudioProvider(api_key="test-api-key-123")
    p._http_client = make_mock_http_client(
        request_id="req-aud-001",
        outcome_key="audio_url",
        outcome_url="https://gmicloud-output.com/speech.mp3",
    )
    return p


# --- Submit ---


def test_submit_returns_submit_result(provider):
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello world")
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)
    assert result.prediction_id == "req-aud-001"


def test_submit_forwards_params(provider):
    step = Step(
        provider="gmicloud-audio",
        model="ElevenLabs-TTS-v3",
        prompt="test",
        params={"voice_id": "abc123", "language": "en"},
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["voice_id"] == "abc123"
    assert body["payload"]["language"] == "en"


# --- Voice cloning (reference audio input) ---


def test_voice_clone_forwards_reference_audio(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-audio",
        model="MiniMax-Voice-Clone-Speech-2.6-HD",
        prompt="Hello world",
        inputs=[Asset(url="https://example.com/sample.mp3", media_type="audio/mpeg")],
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["reference_audio"] == "https://example.com/sample.mp3"


def test_non_clone_model_does_not_forward_reference_audio(provider):
    """Inputs attached to a non-clone model must be ignored (not forwarded)."""
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-audio",
        model="ElevenLabs-TTS-v3",
        prompt="Hello",
        inputs=[Asset(url="https://example.com/sample.mp3", media_type="audio/mpeg")],
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert "reference_audio" not in body["payload"]


def test_voice_clone_rejects_http_reference_audio(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-audio",
        model="MiniMax-Voice-Clone-Speech-2.6-HD",
        prompt="Hello",
        inputs=[Asset(url="http://evil.com/sample.mp3", media_type="audio/mpeg")],
    )
    with pytest.raises(ProviderError, match="[Uu]nsafe"):
        provider.submit(step)


# --- Poll ---


def test_poll_returns_true_on_success(provider):
    assert provider.poll("req-aud-001") is True


def test_poll_returns_false_on_processing(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "processing"}
    provider._http_client.get.return_value = resp
    assert provider.poll("req-aud-001") is False


# --- Fetch output ---


def test_fetch_output_attaches_audio_asset(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello")
    result = provider.fetch_output("req-aud-001", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "audio/mpeg"


def test_audio_metadata_tts_mono(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello")
    result = provider.fetch_output("req-aud-001", step)
    assert result.assets[0].audio is not None
    assert result.assets[0].audio.channels == 1
    assert result.assets[0].audio.codec == "mp3"


def test_audio_metadata_music_stereo(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="MiniMax-Music-2.5", prompt="upbeat jazz")
    result = provider.fetch_output("req-aud-001", step)
    assert result.assets[0].audio.channels == 2


def test_fetch_output_failed_raises(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "failed", "error": "Voice not found"}
    provider._http_client.get.return_value = resp
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="test")
    with pytest.raises(ProviderError, match="Voice not found"):
        provider.fetch_output("req-aud-001", step)


def test_invoke_full_lifecycle(provider):
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello world")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


# --- Cost ---


def test_cost_tracked(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello")
    result = provider.fetch_output("req-aud-001", step)
    assert result.cost_usd == pytest.approx(0.10)


def test_cost_none_unknown_model(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="unknown-tts", prompt="Hello")
    result = provider.fetch_output("req-aud-001", step)
    assert result.cost_usd is None


# --- Payload + security ---


def test_provider_payload_populated(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello")
    result = provider.fetch_output("req-aud-001", step)
    assert result.provider_payload["gmicloud"]["request_id"] == "req-aud-001"


def test_credentials_not_in_provider_payload(provider):
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="Hello")
    result = provider.fetch_output("req-aud-001", step)
    assert "test-api-key-123" not in json.dumps(result.provider_payload)


def test_asset_url_rejects_non_https(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "success", "outcome": {"audio_url": "file:///etc/passwd"}}
    provider._http_client.get.return_value = resp
    provider.poll("req-aud-001")
    step = Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="test")
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("req-aud-001", step)


# --- Normalize params ---


def test_normalize_params_voice(provider):
    result = provider.normalize_params({"voice": "alloy"})
    assert result["voice_id"] == "alloy"
    assert "voice" not in result


def test_normalize_params_idempotent(provider):
    params = {"voice": "alloy"}
    once = provider.normalize_params(params)
    assert provider.normalize_params(once) == once


# --- Passthrough ---


def test_unknown_model_passthrough(provider):
    step = Step(provider="gmicloud-audio", model="NewTTS-v99", prompt="test")
    assert isinstance(provider.submit(step), SubmitResult)


# --- Compliance ---


class TestGMICloudAudioCompliance(ProviderComplianceTests):
    def make_provider(self):
        from genblaze_gmicloud.audio import GMICloudAudioProvider

        p = GMICloudAudioProvider(api_key="test-compliance-key")
        p._http_client = make_mock_http_client(
            request_id="req-aud-001",
            outcome_key="audio_url",
            outcome_url="https://gmicloud-output.com/speech.mp3",
        )
        p.poll_interval = 0.0

        original_submit = p.submit

        def fast_submit(step, config=None):
            r = original_submit(step, config)
            r.estimated_seconds = None
            return r

        p.submit = fast_submit
        return p

    def make_step(self):
        return Step(provider="gmicloud-audio", model="ElevenLabs-TTS-v3", prompt="test prompt")
