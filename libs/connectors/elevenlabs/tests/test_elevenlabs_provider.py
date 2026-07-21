"""Tests for ElevenLabs providers (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode, StepStatus
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


class _FakeApiError(Exception):
    """Mirrors elevenlabs.core.api_error.ApiError — carries status_code."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def test_tts_404_surfaces_as_model_error(mock_tts):
    """A 404 (unknown model_id) must surface on the ProviderError as
    MODEL_ERROR — the code the pipeline's fallback_models retry gates on
    (#167). This asserts the classification only; the actual fallback-firing
    is covered by the pipeline tests in libs/core."""
    provider, client = mock_tts
    client.text_to_speech.convert.side_effect = _FakeApiError("model not found", 404)
    step = Step(provider="elevenlabs-tts", model="eleven_bogus", prompt="test")
    with pytest.raises(ProviderError) as exc_info:
        provider.generate(step)
    assert exc_info.value.error_code == ProviderErrorCode.MODEL_ERROR


def _fake_timestamps_response(text: str = "hi there"):
    """Builds an object matching elevenlabs 2.x's
    ``AudioWithTimestampsResponse`` — a Pydantic model exposing
    ``audio_base_64`` (not ``audio_base64``) and an ``alignment`` object
    (not a dict) with ``characters`` / ``character_start_times_seconds`` /
    ``character_end_times_seconds`` list attributes. See
    elevenlabs.types.audio_with_timestamps_response for the real shape."""
    import base64

    chars = list(text)
    starts = [i * 0.1 for i in range(len(chars))]
    ends = [(i + 1) * 0.1 for i in range(len(chars))]
    alignment = SimpleNamespace(
        characters=chars,
        character_start_times_seconds=starts,
        character_end_times_seconds=ends,
    )
    return SimpleNamespace(
        audio_base_64=base64.b64encode(b"fake-audio").decode(),
        alignment=alignment,
        normalized_alignment=alignment,
    )


def test_tts_with_timestamps_reads_object_response(mock_tts):
    """elevenlabs 2.x's convert_with_timestamps returns a pydantic model,
    not a dict — .get(...) raises AttributeError (#163). generate() must
    read attributes (audio_base_64, alignment.characters, ...) instead."""
    provider, client = mock_tts
    client.text_to_speech.convert_with_timestamps.return_value = _fake_timestamps_response("hi")
    step = Step(
        provider="elevenlabs-tts",
        model="eleven_v3",
        prompt="hi",
        params={"with_timestamps": True},
    )
    result = provider.generate(step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.size_bytes == len(b"fake-audio")
    word_timings = asset.audio.word_timings
    assert word_timings, "expected word_timings populated from alignment"
    assert word_timings[0].word == "hi"


def test_tts_with_timestamps_handles_missing_alignment(mock_tts):
    """``alignment`` is Optional on the real response model — must not
    crash when the API omits it."""
    provider, client = mock_tts
    response = _fake_timestamps_response("hi")
    response.alignment = None
    client.text_to_speech.convert_with_timestamps.return_value = response
    step = Step(
        provider="elevenlabs-tts",
        model="eleven_v3",
        prompt="hi",
        params={"with_timestamps": True},
    )
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert not result.assets[0].audio.word_timings


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


def test_sfx_cost_none_by_default(mock_sfx):
    """As of genblaze-core 0.3.0 the SDK ships zero hardcoded prices.
    SFX users register pricing via ``register_pricing()``; see
    ``docs/reference/pricing-recipes.md`` for the canonical recipe."""
    provider, _ = mock_sfx
    step = Step(
        provider="elevenlabs-sfx",
        model="eleven_text_to_sound_v2",
        prompt="thunder",
        params={"duration_seconds": "3"},
    )
    result = provider.generate(step)
    assert result.cost_usd is None


def test_sfx_cost_tracked_with_user_registered_buckets(mock_sfx):
    """User-registered duration-bucket pricing flows through compute_cost.

    Demonstrates the canonical SFX recipe: ``bucketed_by_duration``
    keyed on ``[lo, hi)`` ranges that map to per-bucket flat rates."""
    from genblaze_core.providers import bucketed_by_duration

    buckets = [
        ((0.0, 5.0 + 1e-9), 0.10),
        ((5.0 + 1e-9, 15.0 + 1e-9), 0.20),
        ((15.0 + 1e-9, 30.0 + 1e-9), 0.30),
    ]
    provider, _ = mock_sfx
    provider._models = provider.models.fork()
    provider.models.register_pricing("eleven_text_to_sound_v2", bucketed_by_duration(buckets))

    for duration_s, expected in (("3", 0.10), ("10", 0.20), ("25", 0.30)):
        step = Step(
            provider="elevenlabs-sfx",
            model="eleven_text_to_sound_v2",
            prompt="x",
            params={"duration_seconds": duration_s},
        )
        result = provider.generate(step)
        assert result.cost_usd == expected, f"duration={duration_s}"


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

    # SDK no longer ships pricing as of genblaze-core 0.3.0.
    expects_cost = False

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
        provider = ElevenLabsTTSProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="elevenlabs-tts", model="eleven_v3", prompt="test prompt")


class TestElevenLabsSFXCompliance(ProviderComplianceTests):
    """Verify ElevenLabsSFXProvider satisfies the genblaze provider contract."""

    # SDK no longer ships pricing as of genblaze-core 0.3.0.
    expects_cost = False

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
        provider = ElevenLabsSFXProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(
            provider="elevenlabs-sfx",
            model="eleven_text_to_sound_v2",
            prompt="test prompt",
            params={"duration_seconds": "5"},
        )
