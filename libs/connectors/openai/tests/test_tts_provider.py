"""Tests for OpenAITTSProvider (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from decimal import Decimal
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


# --- Cost tracking ---
#
# As of genblaze-core 0.3.0 the SDK ships zero hardcoded prices (see
# docs/exec-plans/completed/model-registry-decoupling.md and the
# `test_pricing_phaseout.py` CI guard) — rate tables baked into connectors
# rot faster than releases ship. Cost tracking is opt-in via
# ``provider.models.register_pricing()``; the recipe below mirrors the
# "OpenAI" section of ``docs/reference/pricing-recipes.md`` exactly, so
# these tests double as a regression guard on that doc staying accurate.


def test_estimate_cost_none_by_default():
    """No ModelSpec ships pricing, so estimate_cost() returns None until the
    caller registers a strategy — this is the bug reported in #222/#223."""
    from genblaze_openai.tts import OpenAITTSProvider

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        provider = OpenAITTSProvider(api_key="test-key")
    assert provider.estimate_cost("tts-1", {"prompt": "hello"}) is None


def test_estimate_cost_with_registered_per_char_pricing():
    """Registering the documented ``per_input_chars`` recipe makes
    estimate_cost() return a real Decimal, and cost scales with input size —
    doubling the prompt roughly doubles the estimate."""
    from genblaze_core.providers import per_input_chars
    from genblaze_openai.tts import OpenAITTSProvider

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        provider = OpenAITTSProvider(api_key="test-key")
    # Fork to avoid polluting the class-level models_default() cache.
    provider._models = provider.models.fork()
    # USD per 1M input chars — tts-1 rate from the canonical recipe.
    provider.models.register_pricing("tts-1", per_input_chars(15.00, per=1_000_000))

    short_cost = provider.estimate_cost("tts-1", {"prompt": "hello " * 100})
    long_cost = provider.estimate_cost("tts-1", {"prompt": "hello " * 200})

    assert isinstance(short_cost, Decimal)
    assert isinstance(long_cost, Decimal)
    assert long_cost == pytest.approx(short_cost * 2, rel=0.01)


def test_cost_tracked_on_generated_step(mock_tts):
    """User-registered pricing also flows through generate() → cost_usd,
    not just the estimate_cost() preview path. ``cost_usd`` is a plain
    ``float`` on ``Step`` (unlike ``estimate_cost()``'s ``Decimal``)."""
    from genblaze_core.providers import per_input_chars

    provider, _ = mock_tts
    provider._models = provider.models.fork()
    provider.models.register_pricing("tts-1", per_input_chars(15.00, per=1_000_000))

    step = Step(provider="openai-tts", model="tts-1", prompt="hello " * 1000)
    result = provider.generate(step)
    assert result.cost_usd == pytest.approx(len(step.prompt) / 1_000_000 * 15.00)


# --- Compliance harness ---


class TestOpenAITTSCompliance(ProviderComplianceTests):
    """Verify OpenAITTSProvider satisfies the genblaze provider contract."""

    # SDK no longer ships pricing as of genblaze-core 0.3.0.
    expects_cost = False

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
