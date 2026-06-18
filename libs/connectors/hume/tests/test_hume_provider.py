"""Tests for HumeTTSProvider (mocked — no real API calls)."""

from __future__ import annotations

import base64
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


def _fake_response(raw: bytes = b"fake-audio-data", *, sample_rate: int = 48000):
    """Build an OctaveResponse-shaped object with one base64 generation."""
    generation = SimpleNamespace(
        audio=base64.b64encode(raw).decode(),
        duration=1.5,
        encoding=SimpleNamespace(format="mp3", sample_rate=sample_rate),
        file_size=len(raw),
        generation_id="gen-123",
    )
    return SimpleNamespace(generations=[generation], request_id="req-1")


@pytest.fixture
def mock_hume(tmp_path):
    """Patch the hume SDK modules and inject a mock client."""
    fake_tts = MagicMock()  # provides PostedUtterance / PostedUtteranceVoiceWithName
    with patch.dict(sys.modules, {"hume": MagicMock(), "hume.tts": fake_tts}):
        from genblaze_hume import HumeTTSProvider

        mock_client = MagicMock()
        mock_client.tts.synthesize_json = MagicMock(return_value=_fake_response())
        provider = HumeTTSProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client, fake_tts


# --- happy path -----------------------------------------------------------


def test_generate_returns_audio_asset(mock_hume):
    provider, _, _ = mock_hume
    step = Step(provider="hume-tts", model="octave-2", prompt="Hello world")
    result = provider.generate(step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "audio/mpeg"
    assert asset.url.startswith("file://")
    assert asset.duration == 1.5
    assert asset.audio is not None
    assert asset.audio.channels == 1
    assert asset.audio.codec == "mp3"
    assert asset.audio.sample_rate == 48000
    assert asset.metadata["audio_type"] == "speech"


def test_invoke_full_lifecycle(mock_hume):
    provider, _, _ = mock_hume
    step = Step(provider="hume-tts", model="octave-2", prompt="Hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_base64_decoded_to_file(mock_hume, tmp_path):
    provider, _, _ = mock_hume
    step = Step(provider="hume-tts", model="octave-2", prompt="Hi")
    result = provider.generate(step)
    path = result.assets[0].url.removeprefix("file://")
    from urllib.parse import unquote

    assert open(unquote(path), "rb").read() == b"fake-audio-data"


# --- model → version mapping ---------------------------------------------


@pytest.mark.parametrize(
    "model,expected_version",
    [("octave-1", "1"), ("octave-2", "2")],
)
def test_model_maps_to_version(mock_hume, model, expected_version):
    provider, client, _ = mock_hume
    step = Step(provider="hume-tts", model=model, prompt="x")
    provider.generate(step)
    kwargs = client.tts.synthesize_json.call_args.kwargs
    assert kwargs["version"] == expected_version


def test_unmapped_octave_slug_omits_version(mock_hume):
    provider, client, _ = mock_hume
    step = Step(provider="hume-tts", model="octave-custom-preview", prompt="x")
    provider.generate(step)
    kwargs = client.tts.synthesize_json.call_args.kwargs
    assert "version" not in kwargs


# --- param mapping --------------------------------------------------------


def test_voice_id_aliased_and_passed(mock_hume):
    provider, _, fake_tts = mock_hume
    step = Step(
        provider="hume-tts",
        model="octave-2",
        prompt="test",
        params={"voice_id": "Ava Song"},
    )
    provider.generate(step)
    # voice_id → voice (alias), forwarded to the Hume voice-ref constructor.
    fake_tts.PostedUtteranceVoiceWithName.assert_called_once()
    assert fake_tts.PostedUtteranceVoiceWithName.call_args.kwargs["name"] == "Ava Song"


def test_output_format_maps_to_wav(mock_hume):
    provider, client, _ = mock_hume
    step = Step(
        provider="hume-tts",
        model="octave-2",
        prompt="test",
        params={"output_format": "wav"},
    )
    result = provider.generate(step)
    assert client.tts.synthesize_json.call_args.kwargs["format"] == {"type": "wav"}
    assert result.assets[0].media_type == "audio/wav"
    assert result.assets[0].url.endswith(".wav")


def test_normalize_params_idempotent(mock_hume):
    provider, _, _ = mock_hume
    params = {"voice_id": "Ava", "output_format": "mp3", "speed": 1.1}
    once = provider.normalize_params(params)
    twice = provider.normalize_params(once)
    assert once == twice


def test_unsupported_output_format_raises(mock_hume):
    provider, _, _ = mock_hume
    step = Step(
        provider="hume-tts",
        model="octave-2",
        prompt="test",
        params={"output_format": "ogg"},
    )
    with pytest.raises(ProviderError, match="Unsupported output_format") as ei:
        provider.generate(step)
    assert ei.value.error_code == ProviderErrorCode.INVALID_INPUT


# --- credentials ----------------------------------------------------------


def test_missing_api_key_raises_auth_failure(monkeypatch):
    """No api_key and no HUME_API_KEY → fail fast with AUTH_FAILURE, not a
    deferred opaque error from a keyless client."""
    monkeypatch.delenv("HUME_API_KEY", raising=False)
    with patch.dict(sys.modules, {"hume": MagicMock(), "hume.tts": MagicMock()}):
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key=None)  # no injected client
        step = Step(provider="hume-tts", model="octave-2", prompt="hi")
        with pytest.raises(ProviderError, match="No Hume API key") as ei:
            provider.generate(step)
        assert ei.value.error_code == ProviderErrorCode.AUTH_FAILURE


# --- list_voices ----------------------------------------------------------


def test_list_voices_maps_catalog(mock_hume):
    provider, client, _ = mock_hume
    client.tts.voices.list.return_value = [
        SimpleNamespace(id="v1", name="Ava", compatible_octave_models=["octave-2"]),
        SimpleNamespace(id="v2", name="Ben", compatible_octave_models=["octave-1"]),
    ]
    voices = provider.list_voices()
    assert [v.voice_id for v in voices] == ["v1", "v2"]
    assert all(v.provider == "hume-tts" for v in voices)


def test_list_voices_filters_by_compatible_model(mock_hume):
    provider, client, _ = mock_hume
    client.tts.voices.list.return_value = [
        SimpleNamespace(id="v1", name="Ava", compatible_octave_models=["octave-2"]),
        SimpleNamespace(id="v2", name="Ben", compatible_octave_models=["octave-1"]),
    ]
    voices = provider.list_voices(model="octave-2")
    assert [v.voice_id for v in voices] == ["v1"]


def test_list_voices_degrades_to_empty_on_error(mock_hume):
    provider, client, _ = mock_hume
    client.tts.voices.list.side_effect = RuntimeError("transport down")
    assert provider.list_voices() == []


# --- cost -----------------------------------------------------------------


def test_cost_none_by_default(mock_hume):
    """The SDK ships zero hardcoded prices — cost is None until registered."""
    provider, _, _ = mock_hume
    step = Step(provider="hume-tts", model="octave-2", prompt="Hello world")
    result = provider.generate(step)
    assert result.cost_usd is None


def test_cost_tracked_with_user_registered_pricing(mock_hume):
    from genblaze_core.providers import per_input_chars

    provider, _, _ = mock_hume
    provider.models.register_pricing("octave-2", per_input_chars(0.0002, per=1))
    step = Step(provider="hume-tts", model="octave-2", prompt="Hello world")
    result = provider.generate(step)
    assert result.cost_usd == pytest.approx(len("Hello world") * 0.0002)


# --- error mapping --------------------------------------------------------


def test_api_error_raises_provider_error(mock_hume):
    provider, client, _ = mock_hume
    err = RuntimeError("boom")
    err.status_code = 429  # type: ignore[attr-defined]
    client.tts.synthesize_json.side_effect = err
    step = Step(provider="hume-tts", model="octave-2", prompt="x")
    with pytest.raises(ProviderError, match="Hume TTS failed") as ei:
        provider.generate(step)
    assert ei.value.error_code == ProviderErrorCode.RATE_LIMIT


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, ProviderErrorCode.RATE_LIMIT),
        (401, ProviderErrorCode.AUTH_FAILURE),
        (403, ProviderErrorCode.AUTH_FAILURE),
        (400, ProviderErrorCode.INVALID_INPUT),
        (422, ProviderErrorCode.INVALID_INPUT),
        (503, ProviderErrorCode.SERVER_ERROR),
    ],
)
def test_map_hume_error_status_codes(status, expected):
    from genblaze_hume._errors import map_hume_error

    exc = RuntimeError("err")
    exc.status_code = status  # type: ignore[attr-defined]
    assert map_hume_error(exc) == expected


def test_map_hume_error_no_status_falls_back():
    from genblaze_hume._errors import map_hume_error

    # No status_code attribute → string-based classifier fallback.
    assert map_hume_error(RuntimeError("totally opaque")) == ProviderErrorCode.UNKNOWN


# --- catalog-decoupling proof-point --------------------------------------


def test_declares_discovery_support_none():
    from genblaze_core.providers import DiscoverySupport
    from genblaze_hume import HumeTTSProvider

    assert HumeTTSProvider.discovery_support is DiscoverySupport.NONE


def test_octave_slug_matches_family(mock_hume):
    """``octave-*`` slugs match the family → OK_PROVISIONAL at preflight."""
    from genblaze_core.providers import ValidationOutcome

    provider, _, _ = mock_hume
    result = provider.validate_model("octave-2")
    assert result.outcome is ValidationOutcome.OK_PROVISIONAL


def test_non_octave_slug_unknown_permissive(mock_hume):
    """Non-octave slugs fall through the permissive fallback."""
    from genblaze_core.providers import ValidationOutcome

    provider, _, _ = mock_hume
    result = provider.validate_model("some-other-model")
    assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


def test_family_and_fallback_carry_no_pricing(mock_hume):
    provider, _, _ = mock_hume
    assert provider._models.get("octave-2").pricing is None
    assert provider._models.get("anything-else").pricing is None


# --- compliance harness ---------------------------------------------------


class TestHumeCompliance(ProviderComplianceTests):
    """Verify HumeTTSProvider satisfies the genblaze provider contract."""

    # As of genblaze-core 0.3.0 the SDK ships zero hardcoded prices.
    # Hume users register pricing via ``provider.models.register_pricing()``;
    # see ``docs/reference/pricing-recipes.md``.
    expects_cost = False

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict(sys.modules, {"hume": MagicMock(), "hume.tts": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_hume import HumeTTSProvider

        mock_client = MagicMock()
        mock_client.tts.synthesize_json = MagicMock(return_value=_fake_response())
        provider = HumeTTSProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="hume-tts", model="octave-2", prompt="test prompt")
