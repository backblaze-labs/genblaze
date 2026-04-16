"""Tests for OpenAITTSProvider (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_tts(tmp_path):
    """Patch openai with a mock client that writes a dummy audio file."""
    mock_response = MagicMock()
    mock_response.write_to_file = MagicMock(
        side_effect=lambda path: open(path, "wb").write(b"fake-audio-data")
    )

    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai.tts import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


def test_generate_returns_audio_asset(mock_tts):
    provider, _ = mock_tts
    step = Step(provider="openai-tts", model="tts-1", prompt="Hello world")
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "audio/mpeg"
    assert result.assets[0].url.startswith("file://")


def test_invoke_full_lifecycle(mock_tts):
    provider, _ = mock_tts
    step = Step(provider="openai-tts", model="tts-1", prompt="Hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_custom_voice_and_format(mock_tts):
    provider, client = mock_tts
    step = Step(
        provider="openai-tts",
        model="tts-1-hd",
        prompt="test",
        params={"voice": "nova", "response_format": "wav"},
    )
    result = provider.generate(step)
    assert result.assets[0].media_type == "audio/wav"
    call_kwargs = client.audio.speech.create.call_args[1]
    assert call_kwargs["voice"] == "nova"
    assert call_kwargs["response_format"] == "wav"


def test_speed_param_passed(mock_tts):
    provider, client = mock_tts
    step = Step(
        provider="openai-tts",
        model="tts-1",
        prompt="fast",
        params={"speed": "1.5"},
    )
    provider.generate(step)
    call_kwargs = client.audio.speech.create.call_args[1]
    assert call_kwargs["speed"] == 1.5


def test_audio_type_metadata(mock_tts):
    """OpenAI TTS assets are tagged as speech."""
    provider, _ = mock_tts
    step = Step(provider="openai-tts", model="tts-1", prompt="Hello")
    result = provider.generate(step)
    assert result.assets[0].metadata["audio_type"] == "speech"


def test_audio_metadata_populated(mock_tts):
    """OpenAI TTS assets carry AudioMetadata with codec and channels."""
    provider, _ = mock_tts
    step = Step(provider="openai-tts", model="tts-1", prompt="Hello")
    result = provider.generate(step)
    assert result.assets[0].audio is not None
    assert result.assets[0].audio.codec == "mp3"
    assert result.assets[0].audio.channels == 1


def test_audio_metadata_custom_format(mock_tts):
    """AudioMetadata codec matches the requested response_format."""
    provider, _ = mock_tts
    step = Step(
        provider="openai-tts",
        model="tts-1",
        prompt="test",
        params={"response_format": "flac"},
    )
    result = provider.generate(step)
    assert result.assets[0].audio is not None
    assert result.assets[0].audio.codec == "flac"


def test_api_error_raises_provider_error(mock_tts):
    provider, client = mock_tts
    client.audio.speech.create.side_effect = RuntimeError("401 unauthorized")
    step = Step(provider="openai-tts", model="tts-1", prompt="test")
    with pytest.raises(ProviderError, match="TTS generation failed"):
        provider.generate(step)


# --- Compliance harness ---


class TestOpenAITTSCompliance(ProviderComplianceTests):
    """Verify OpenAITTSProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"openai": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_openai.tts import OpenAITTSProvider

        mock_response = MagicMock()
        mock_response.write_to_file = MagicMock(
            side_effect=lambda path: open(path, "wb").write(b"fake-audio-data")
        )
        mock_client = MagicMock()
        mock_client.audio.speech.create.return_value = mock_response
        provider = OpenAITTSProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="openai-tts", model="tts-1", prompt="test prompt")
