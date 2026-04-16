"""Tests for ElevenLabs providers (mocked — no real API calls)."""

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
    """Patch elevenlabs with a mock client for TTS."""
    mock_client = MagicMock()
    mock_client.text_to_speech.convert.return_value = iter([b"fake-audio"])

    with patch.dict("sys.modules", {"elevenlabs": MagicMock(), "elevenlabs.client": MagicMock()}):
        from genblaze_elevenlabs.provider import ElevenLabsTTSProvider

        provider = ElevenLabsTTSProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


@pytest.fixture
def mock_sfx(tmp_path):
    """Patch elevenlabs with a mock client for SFX."""
    mock_client = MagicMock()
    mock_client.text_to_sound_effects.convert.return_value = iter([b"fake-sfx"])

    with patch.dict("sys.modules", {"elevenlabs": MagicMock(), "elevenlabs.client": MagicMock()}):
        from genblaze_elevenlabs.sfx import ElevenLabsSFXProvider

        provider = ElevenLabsSFXProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


# --- TTS Tests ---


def test_tts_generate_returns_audio_asset(mock_tts):
    provider, _ = mock_tts
    step = Step(provider="elevenlabs-tts", model="eleven_v3", prompt="Hello world")
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "audio/mpeg"
    assert result.assets[0].url.startswith("file://")


def test_tts_invoke_full_lifecycle(mock_tts):
    provider, _ = mock_tts
    step = Step(provider="elevenlabs-tts", model="eleven_v3", prompt="Hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_tts_voice_settings_passed(mock_tts):
    provider, client = mock_tts
    step = Step(
        provider="elevenlabs-tts",
        model="eleven_multilingual_v2",
        prompt="test",
        params={
            "voice_id": "custom-voice-id",
            "stability": "0.7",
            "similarity_boost": "0.8",
        },
    )
    provider.generate(step)
    call_kwargs = client.text_to_speech.convert.call_args[1]
    assert call_kwargs["voice_id"] == "custom-voice-id"
    assert call_kwargs["voice_settings"]["stability"] == 0.7


def test_tts_audio_type_metadata(mock_tts):
    """TTS assets are tagged as speech."""
    provider, _ = mock_tts
    step = Step(provider="elevenlabs-tts", model="eleven_v3", prompt="Hello")
    result = provider.generate(step)
    assert result.assets[0].metadata["audio_type"] == "speech"


def test_tts_api_error_raises(mock_tts):
    provider, client = mock_tts
    client.text_to_speech.convert.side_effect = RuntimeError("429 rate limit")
    step = Step(provider="elevenlabs-tts", model="eleven_v3", prompt="test")
    with pytest.raises(ProviderError, match="ElevenLabs TTS failed"):
        provider.generate(step)


# --- SFX Tests ---


def test_sfx_generate_returns_audio_asset(mock_sfx):
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="thunder crashing",
    )
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "audio/mpeg"


def test_sfx_invoke_full_lifecycle(mock_sfx):
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="rain on window",
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED


def test_sfx_invalid_duration_raises(mock_sfx):
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="test",
        params={"duration_seconds": "50"},
    )
    with pytest.raises(ProviderError, match="Invalid duration_seconds"):
        provider.generate(step)


def test_sfx_audio_type_metadata(mock_sfx):
    """SFX assets are tagged as sfx."""
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="thunder",
    )
    result = provider.generate(step)
    assert result.assets[0].metadata["audio_type"] == "sfx"


def test_sfx_asset_duration_set(mock_sfx):
    """SFX asset duration is set from params."""
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="rain",
        params={"duration_seconds": "10"},
    )
    result = provider.generate(step)
    assert result.assets[0].duration == 10.0


def test_sfx_cost_tracked_short(mock_sfx):
    """Short SFX (<=5s) uses short price bucket."""
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="thunder",
        params={"duration_seconds": "3"},
    )
    result = provider.generate(step)
    assert result.cost_usd == 0.10


def test_sfx_cost_tracked_medium(mock_sfx):
    """Medium SFX (5-15s) uses medium price bucket."""
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="rain",
        params={"duration_seconds": "10"},
    )
    result = provider.generate(step)
    assert result.cost_usd == 0.20


def test_sfx_cost_tracked_long(mock_sfx):
    """Long SFX (15-30s) uses long price bucket."""
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="ambient",
        params={"duration_seconds": "25"},
    )
    result = provider.generate(step)
    assert result.cost_usd == 0.30


def test_sfx_api_error_raises(mock_sfx):
    provider, client = mock_sfx
    client.text_to_sound_effects.convert.side_effect = RuntimeError("401 unauthorized")
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="test",
    )
    with pytest.raises(ProviderError, match="ElevenLabs SFX failed"):
        provider.generate(step)


# --- Compliance harness ---


class TestElevenLabsTTSCompliance(ProviderComplianceTests):
    """Verify ElevenLabsTTSProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict(
            "sys.modules",
            {"elevenlabs": MagicMock(), "elevenlabs.client": MagicMock()},
        ):
            yield

    def make_provider(self):
        from genblaze_elevenlabs.provider import ElevenLabsTTSProvider

        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = iter([b"fake-audio"])
        provider = ElevenLabsTTSProvider(
            api_key="test-key", output_dir=tempfile.mkdtemp()
        )
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(
            provider="elevenlabs-tts", model="eleven_v3", prompt="test prompt"
        )


class TestElevenLabsSFXCompliance(ProviderComplianceTests):
    """Verify ElevenLabsSFXProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict(
            "sys.modules",
            {"elevenlabs": MagicMock(), "elevenlabs.client": MagicMock()},
        ):
            yield

    def make_provider(self):
        from genblaze_elevenlabs.sfx import ElevenLabsSFXProvider

        mock_client = MagicMock()
        mock_client.text_to_sound_effects.convert.return_value = iter([b"fake-sfx"])
        provider = ElevenLabsSFXProvider(
            api_key="test-key", output_dir=tempfile.mkdtemp()
        )
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(
            provider="elevenlabs-sfx",
            model="eleven_text_to_sound_v2",
            prompt="test prompt",
            params={"duration_seconds": "5"},
        )
