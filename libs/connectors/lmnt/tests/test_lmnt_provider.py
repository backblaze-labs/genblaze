"""Tests for LMNTProvider (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_lmnt(tmp_path):
    """Patch lmnt with a mock client."""
    mock_client = MagicMock()
    mock_client.synthesize = AsyncMock(return_value={"audio": b"fake-audio-data", "durations": []})
    mock_client.close = AsyncMock()

    mock_lmnt_mod = MagicMock()

    with patch.dict("sys.modules", {"lmnt": mock_lmnt_mod, "lmnt.api": MagicMock()}):
        from genblaze_lmnt import LMNTProvider

        provider = LMNTProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._speech_client = mock_client
        yield provider, mock_client


def test_generate_returns_audio_asset(mock_lmnt):
    provider, _ = mock_lmnt
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello world")
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "audio/mpeg"
    assert result.assets[0].url.startswith("file://")


def test_invoke_full_lifecycle(mock_lmnt):
    provider, _ = mock_lmnt
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_voice_param_passed(mock_lmnt):
    provider, client = mock_lmnt
    step = Step(
        provider="lmnt",
        model="lmnt-1",
        prompt="test",
        params={"voice": "custom-voice-id", "speed": "1.2"},
    )
    provider.generate(step)
    call_kwargs = client.synthesize.call_args[1]
    assert call_kwargs["voice"] == "custom-voice-id"
    assert call_kwargs["speed"] == 1.2


def test_durations_stored_in_payload(mock_lmnt):
    provider, client = mock_lmnt
    client.synthesize = AsyncMock(
        return_value={
            "audio": b"fake-audio",
            "durations": [{"text": "Hello", "start": 0, "end": 0.5}],
        }
    )
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello")
    result = provider.generate(step)
    assert result.provider_payload["lmnt"]["durations"] is not None
    # Word timings stored as typed WordTiming objects on asset.audio
    assert result.assets[0].audio is not None
    assert result.assets[0].audio.word_timings is not None
    assert result.assets[0].audio.word_timings[0].word == "Hello"
    assert result.assets[0].duration == 0.5


def test_audio_type_metadata(mock_lmnt):
    """LMNT assets are tagged as speech."""
    provider, _ = mock_lmnt
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello")
    result = provider.generate(step)
    assert result.assets[0].metadata["audio_type"] == "speech"


def test_multi_word_duration(mock_lmnt):
    """Duration is max of all word end times."""
    provider, client = mock_lmnt
    client.synthesize = AsyncMock(
        return_value={
            "audio": b"fake-audio",
            "durations": [
                {"text": "Hello", "start": 0, "end": 0.4},
                {"text": "beautiful", "start": 0.4, "end": 0.9},
                {"text": "world", "start": 0.9, "end": 1.3},
            ],
        }
    )
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello beautiful world")
    result = provider.generate(step)
    assert result.assets[0].duration == 1.3
    assert result.assets[0].audio is not None
    assert len(result.assets[0].audio.word_timings) == 3
    assert result.assets[0].audio.word_timings[2].word == "world"


def test_cost_tracked(mock_lmnt):
    """Cost is computed from character count."""
    provider, _ = mock_lmnt
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello world")
    result = provider.generate(step)
    assert result.cost_usd is not None
    assert result.cost_usd == pytest.approx(len("Hello world") * 0.00015)


def test_cost_none_empty_prompt(mock_lmnt):
    """Cost stays None for empty prompt."""
    provider, _ = mock_lmnt
    step = Step(provider="lmnt", model="lmnt-1", prompt="")
    result = provider.generate(step)
    assert result.cost_usd is None


def test_api_error_raises(mock_lmnt):
    provider, client = mock_lmnt
    client.synthesize = AsyncMock(side_effect=RuntimeError("401 unauthorized"))
    step = Step(provider="lmnt", model="lmnt-1", prompt="test")
    with pytest.raises(ProviderError, match="LMNT TTS failed"):
        provider.generate(step)


# --- Compliance harness ---


class TestLMNTCompliance(ProviderComplianceTests):
    """Verify LMNTProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict(
            "sys.modules", {"lmnt": MagicMock(), "lmnt.api": MagicMock()}
        ):
            yield

    def make_provider(self):
        from genblaze_lmnt import LMNTProvider

        mock_client = MagicMock()
        mock_client.synthesize = AsyncMock(
            return_value={"audio": b"fake-audio-data", "durations": []}
        )
        mock_client.close = AsyncMock()
        provider = LMNTProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._speech_client = mock_client
        return provider

    def make_step(self):
        return Step(provider="lmnt", model="lmnt-1", prompt="test prompt")
