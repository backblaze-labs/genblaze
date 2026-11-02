"""Tests for LiteLLMSpeechProvider (mocked; no billable requests)."""

from __future__ import annotations

import hashlib

import pytest
from genblaze_core.models.enums import Modality, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests
from genblaze_litellm import LiteLLMSpeechProvider

from .conftest import AUDIO_BYTES


def _step(**params):
    return Step(
        provider="litellm-speech",
        model="openai/gpt-4o-mini-tts",
        modality=Modality.AUDIO,
        prompt="The tide is coming in.",
        params=params,
    )


def test_speech_defaults_and_asset(fake_litellm, tmp_path):
    result = LiteLLMSpeechProvider(output_dir=tmp_path).invoke(_step())

    assert result.status == StepStatus.SUCCEEDED
    asset = result.assets[0]
    assert asset.media_type == "audio/mpeg"
    assert asset.url.endswith(".mp3")
    assert asset.sha256 == hashlib.sha256(AUDIO_BYTES).hexdigest()
    assert asset.audio is not None
    assert asset.metadata["audio_type"] == "speech"
    assert result.cost_usd == pytest.approx(0.0003)

    call = fake_litellm.kwargs_of("speech")[0]
    assert call["model"] == "openai/gpt-4o-mini-tts"
    assert call["input"] == "The tide is coming in."
    assert call["voice"] == "alloy"
    assert call["response_format"] == "mp3"
    assert call["drop_params"] is True


def test_voice_format_and_extra_params(fake_litellm, tmp_path):
    result = LiteLLMSpeechProvider(output_dir=tmp_path).invoke(
        _step(voice="coral", response_format="wav", speed="1.25", instructions="calm")
    )
    assert result.assets[0].media_type == "audio/wav"
    call = fake_litellm.kwargs_of("speech")[0]
    assert call["voice"] == "coral"
    assert call["speed"] == 1.25
    assert call["instructions"] == "calm"


def test_provider_error_fails_step_with_code(fake_litellm):
    import litellm

    fake_litellm.raise_on["speech"] = litellm.AuthenticationError(
        message="bad key", llm_provider="openai", model="gpt-4o-mini-tts"
    )
    result = LiteLLMSpeechProvider().invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == "auth_failure"


class TestLiteLLMSpeechCompliance(ProviderComplianceTests):
    @pytest.fixture(autouse=True)
    def _fake(self, fake_litellm):
        self.fake = fake_litellm

    def make_provider(self):
        return LiteLLMSpeechProvider()

    def make_step(self):
        return _step()
