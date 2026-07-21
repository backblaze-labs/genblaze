"""Tests for LMNTProvider (mocked — no real API calls)."""

from __future__ import annotations

import base64
import tempfile
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests
from lmnt.types.speech_generate_detailed_response import Duration, SpeechGenerateDetailedResponse


def _detailed_response(audio: bytes = b"fake-audio-data", durations=None):
    """Build a real ``lmnt`` 2.x ``generate_detailed()`` response.

    Using the actual pydantic response type (rather than a dict or
    MagicMock) keeps these tests honest about the real SDK's response
    shape — see issue #166.
    """
    return SpeechGenerateDetailedResponse(
        audio=base64.b64encode(audio).decode(),
        seed=0,
        durations=durations,
    )


@pytest.fixture
def mock_lmnt(tmp_path):
    """Patch ``LMNTProvider`` with a mock lmnt 2.x ``Lmnt`` client."""
    mock_client = MagicMock()
    mock_client.speech.generate_detailed = MagicMock(return_value=_detailed_response())
    mock_client.close = MagicMock()

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
        params={"voice": "custom-voice-id"},
    )
    provider.generate(step)
    call_kwargs = client.speech.generate_detailed.call_args[1]
    assert call_kwargs["voice"] == "custom-voice-id"


def test_speed_param_dropped_with_warning(mock_lmnt, caplog):
    """``speed`` has no lmnt 2.x equivalent — it must not be forwarded
    (the real SDK would raise TypeError on an unknown kwarg), and the
    user should be warned rather than have it silently vanish."""
    provider, client = mock_lmnt
    step = Step(
        provider="lmnt",
        model="lmnt-1",
        prompt="test",
        params={"speed": "1.2"},
    )
    with caplog.at_level("WARNING", logger="genblaze.lmnt"):
        provider.generate(step)
    call_kwargs = client.speech.generate_detailed.call_args[1]
    assert "speed" not in call_kwargs
    # Warning must name the 2.x replacement knobs so it's actionable, not
    # just an announcement that something got dropped.
    assert any(
        "speed" in rec.message and "temperature" in rec.message and "top_p" in rec.message
        for rec in caplog.records
    )


def test_durations_stored_in_payload(mock_lmnt):
    provider, client = mock_lmnt
    client.speech.generate_detailed = MagicMock(
        return_value=_detailed_response(
            durations=[Duration(text="Hello", start=0, duration=0.5)],
        )
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
    client.speech.generate_detailed = MagicMock(
        return_value=_detailed_response(
            durations=[
                Duration(text="Hello", start=0, duration=0.4),
                Duration(text="beautiful", start=0.4, duration=0.5),
                Duration(text="world", start=0.9, duration=0.4),
            ],
        )
    )
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello beautiful world")
    result = provider.generate(step)
    assert result.assets[0].duration == pytest.approx(1.3)
    assert result.assets[0].audio is not None
    assert len(result.assets[0].audio.word_timings) == 3
    assert result.assets[0].audio.word_timings[2].word == "world"


def test_cost_none_by_default(mock_lmnt):
    """As of 0.3.0, the SDK no longer ships pricing for LMNT.

    ``cost_usd`` is ``None`` unless the user has registered a pricing
    strategy via ``provider.models.register_pricing()``. See
    ``docs/reference/pricing-recipes.md`` for the canonical recipe.
    """
    provider, _ = mock_lmnt
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello world")
    result = provider.generate(step)
    assert result.cost_usd is None


def test_cost_tracked_with_user_registered_pricing(mock_lmnt):
    """User-registered pricing flows through the same compute_cost path."""
    from genblaze_core.providers import per_input_chars

    provider, _ = mock_lmnt
    provider.models.register_pricing("lmnt-1", per_input_chars(0.00015, per=1))
    step = Step(provider="lmnt", model="lmnt-1", prompt="Hello world")
    result = provider.generate(step)
    assert result.cost_usd is not None
    assert result.cost_usd == pytest.approx(len("Hello world") * 0.00015)


def test_cost_none_empty_prompt(mock_lmnt):
    """Cost stays None for empty prompt even with pricing registered."""
    from genblaze_core.providers import per_input_chars

    provider, _ = mock_lmnt
    provider.models.register_pricing("lmnt-1", per_input_chars(0.00015, per=1))
    step = Step(provider="lmnt", model="lmnt-1", prompt="")
    result = provider.generate(step)
    assert result.cost_usd is None


def test_api_error_raises(mock_lmnt):
    provider, client = mock_lmnt
    client.speech.generate_detailed = MagicMock(side_effect=RuntimeError("401 unauthorized"))
    step = Step(provider="lmnt", model="lmnt-1", prompt="test")
    with pytest.raises(ProviderError, match="LMNT TTS failed"):
        provider.generate(step)


# --- Real SDK import surface (issue #166 regression) -----------------------
#
# The fixtures above inject a mock client directly onto ``_speech_client``,
# so they'd happily pass even if ``_make_client()`` imported a module/class
# that doesn't exist in the real, installed ``lmnt`` package. This test
# deliberately exercises ``_make_client()`` against whatever version of
# ``lmnt`` is actually installed (a real dependency of this package), so a
# future import-path drift (like #166: ``lmnt.api.Speech`` removed in lmnt
# 2.x) fails here instead of shipping silently.


def test_make_client_matches_installed_sdk():
    """``_make_client()`` must import successfully against the real,
    installed ``lmnt`` package — not a mocked stand-in."""
    from genblaze_lmnt import LMNTProvider
    from lmnt import Lmnt

    provider = LMNTProvider(api_key="test-key")
    client = provider._make_client()
    assert isinstance(client, Lmnt)
    client.close()


# --- Catalog-decoupling proof-point ---


def test_lmnt_declares_discovery_support_none():
    """LMNT is the proof-point connector for the catalog-decoupled
    architecture: empty defaults, permissive fallback, no discovery API.
    """
    from genblaze_core.providers import DiscoverySupport
    from genblaze_lmnt import LMNTProvider

    assert LMNTProvider.discovery_support is DiscoverySupport.NONE


def test_lmnt_validate_model_returns_unknown_permissive(mock_lmnt):
    """Without a registered slug or family, every LMNT model id falls
    through to the permissive fallback. Pipeline preflight handles this
    with a one-time WARN and proceeds."""
    from genblaze_core.providers import ValidationOutcome

    provider, _ = mock_lmnt
    result = provider.validate_model("any-lmnt-slug")
    assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


def test_lmnt_user_registered_slug_authoritative(mock_lmnt):
    """Once the user registers a slug, validate_model returns
    OK_AUTHORITATIVE — the SDK has positive confirmation regardless of
    LMNT's lack of a discovery API."""
    from genblaze_core.models.enums import Modality
    from genblaze_core.providers import ModelSpec, ValidationOutcome

    provider, _ = mock_lmnt
    provider.models.register(ModelSpec(model_id="lmnt-1", modality=Modality.AUDIO))
    result = provider.validate_model("lmnt-1")
    assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE


# --- Compliance harness ---


class TestLMNTCompliance(ProviderComplianceTests):
    """Verify LMNTProvider satisfies the genblaze provider contract."""

    # As of genblaze-core 0.3.0 the SDK ships zero hardcoded prices.
    # LMNT users register pricing via ``provider.models.register_pricing()``;
    # see ``docs/reference/pricing-recipes.md``.
    expects_cost = False

    def make_provider(self):
        from genblaze_lmnt import LMNTProvider

        mock_client = MagicMock()
        mock_client.speech.generate_detailed = MagicMock(return_value=_detailed_response())
        mock_client.close = MagicMock()
        provider = LMNTProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._speech_client = mock_client
        return provider

    def make_step(self):
        return Step(provider="lmnt", model="lmnt-1", prompt="test prompt")
