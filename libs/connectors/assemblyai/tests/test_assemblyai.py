"""Tests for AssemblyAIProvider (mocked — no real API calls).

The provider's "client" is the ``assemblyai`` module itself (transcription
goes through ``aai.Transcriber()`` / ``aai.Transcript.get_by_id()`` with the
key on ``aai.settings.api_key``). Tests inject a fake module via
``provider._client`` — the lazy ``import assemblyai`` in ``_get_client`` is
never reached on the happy path, so the real SDK is not required.
"""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests

AUDIO_URL = "https://example.com/audio.mp3"


# --- fakes ----------------------------------------------------------------


class _FakeWord:
    def __init__(self, text: str, start: int, end: int, confidence: float = 0.99):
        self.text = text
        self.start = start  # milliseconds (AssemblyAI native)
        self.end = end  # milliseconds
        self.confidence = confidence


def _fake_transcript(
    *,
    status: str = "completed",
    text: str = "hello world",
    with_words: bool = True,
    audio_duration: float | None = 12.5,
    error: str | None = None,
    language_code: str | None = "en_us",
    confidence: float | None = 0.97,
    utterances=None,
):
    words = (
        [_FakeWord("hello", 0, 500, 0.99), _FakeWord("world", 500, 1000, 0.98)]
        if with_words
        else None
    )
    return SimpleNamespace(
        id="transcript-abc123",
        status=status,
        text=text,
        words=words,
        audio_duration=audio_duration,
        language_code=language_code,
        confidence=confidence,
        utterances=utterances,
        error=error,
    )


class _FakeTranscriber:
    """Records the submitted (audio_url, config) and returns a queued id."""

    last_submit: dict = {}

    def submit(self, audio_url, config=None):
        _FakeTranscriber.last_submit = {"audio_url": audio_url, "config": config}
        return SimpleNamespace(id="transcript-abc123", status="queued")


def _make_fake_aai(transcript=None):
    """Build a fake ``assemblyai`` module exposing the surface the provider uses."""
    transcript = transcript if transcript is not None else _fake_transcript()
    fake = SimpleNamespace()
    fake.settings = SimpleNamespace(api_key=None)
    fake.Transcriber = _FakeTranscriber
    fake.Transcript = SimpleNamespace(get_by_id=lambda tid: transcript)
    fake.TranscriptionConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    return fake


def _make_provider(transcript=None):
    from genblaze_assemblyai import AssemblyAIProvider

    provider = AssemblyAIProvider(api_key="test-key", poll_interval=0.0)
    provider._client = _make_fake_aai(transcript)
    return provider


def _make_step(**kwargs) -> Step:
    kwargs.setdefault("provider", "assemblyai")
    kwargs.setdefault("model", "universal-3-pro")
    kwargs.setdefault("modality", Modality.TEXT)
    kwargs.setdefault("prompt", AUDIO_URL)
    return Step(**kwargs)


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """Isolate the class-level model registry between tests.

    ``BaseProvider`` caches one ``ModelRegistry`` per provider class
    (``models_default()``), shared across instances. A test that calls
    ``register_pricing`` would otherwise leak the user-registered spec — and
    flip a family-matched slug's validation outcome to AUTHORITATIVE — into
    later tests. Resetting the cache gives each test a fresh registry built
    from the (unmutated) module-level family/fallback specs.
    """
    from genblaze_assemblyai import AssemblyAIProvider

    AssemblyAIProvider._models_cache = None
    yield
    AssemblyAIProvider._models_cache = None


# --- submit ---------------------------------------------------------------


def test_submit_returns_id_and_forwards_audio_url():
    provider = _make_provider()
    step = _make_step()
    pred_id = provider.submit(step)
    assert pred_id == "transcript-abc123"
    assert _FakeTranscriber.last_submit["audio_url"] == AUDIO_URL


def test_submit_passes_speech_model_to_config():
    provider = _make_provider()
    step = _make_step(model="universal-3-pro")
    provider.submit(step)
    cfg = _FakeTranscriber.last_submit["config"]
    # step.model is sent on the plural ``speech_models`` field — the live API
    # deprecated the singular ``speech_model`` field and the best/nano aliases.
    assert cfg.speech_models == ["universal-3-pro"]


def test_submit_strips_audio_url_from_config():
    provider = _make_provider()
    step = _make_step(prompt=None, params={"audio_url": AUDIO_URL, "speaker_labels": True})
    provider.submit(step)
    cfg = _FakeTranscriber.last_submit["config"]
    assert not hasattr(cfg, "audio_url")
    assert cfg.speaker_labels is True


# --- full lifecycle -------------------------------------------------------


def test_full_lifecycle_via_invoke():
    provider = _make_provider()
    step = _make_step()
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "text/plain"
    assert asset.url.startswith("text:")
    assert asset.metadata["text"] == "hello world"


def test_text_asset_hash_matches_content():
    provider = _make_provider()
    step = _make_step()
    pred_id = provider.submit(step)
    result = provider.fetch_output(pred_id, step)
    asset = result.assets[0]
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert asset.sha256 == expected
    assert asset.url == f"text:{expected}"
    assert asset.size_bytes == len(b"hello world")


# --- word timings (ms -> seconds) ----------------------------------------


def test_word_timings_converted_ms_to_seconds():
    provider = _make_provider()
    step = _make_step()
    pred_id = provider.submit(step)
    result = provider.fetch_output(pred_id, step)
    timings = result.assets[0].audio.word_timings
    assert [t.word for t in timings] == ["hello", "world"]
    # 0 ms -> 0.0 s, 500 ms -> 0.5 s, 1000 ms -> 1.0 s
    assert timings[0].start == 0.0
    assert timings[0].end == 0.5
    assert timings[1].start == 0.5
    assert timings[1].end == 1.0
    assert timings[1].confidence == 0.98


def test_no_words_leaves_audio_metadata_unset():
    provider = _make_provider(_fake_transcript(with_words=False))
    step = _make_step()
    pred_id = provider.submit(step)
    result = provider.fetch_output(pred_id, step)
    assert result.assets[0].audio is None


# --- audio_duration / pricing payload ------------------------------------


def test_audio_duration_captured_in_provider_payload():
    provider = _make_provider(_fake_transcript(audio_duration=42.0))
    step = _make_step()
    pred_id = provider.submit(step)
    result = provider.fetch_output(pred_id, step)
    assert result.provider_payload["audio_duration"] == 42.0
    assert result.assets[0].metadata["audio_duration"] == 42.0


def test_user_registered_per_minute_pricing():
    from genblaze_core.providers import per_response_metric

    provider = _make_provider(_fake_transcript(audio_duration=120.0))  # 2 minutes

    def per_minute(ctx):
        dur = ctx.provider_payload.get("audio_duration")
        return (dur / 60.0) * 0.12 if dur is not None else None

    # "universal-3-pro" matches the speech family, so register against the
    # concrete slug (register_pricing falls through to the family spec and
    # layers pricing).
    provider.models.register_pricing("universal-3-pro", per_response_metric(per_minute))
    step = _make_step(model="universal-3-pro")
    result = provider.invoke(step)
    assert result.cost_usd == pytest.approx(2 * 0.12)


# --- audio-url resolution precedence -------------------------------------


def test_audio_url_precedence_inputs_first():
    provider = _make_provider()
    step = _make_step(
        prompt="https://prompt.example/p.mp3",
        params={"audio_url": "https://params.example/p.mp3"},
        inputs=[Asset(url="https://inputs.example/in.mp3", media_type="audio/mpeg")],
    )
    assert provider._resolve_audio_url(step) == "https://inputs.example/in.mp3"


def test_audio_url_precedence_params_over_prompt():
    provider = _make_provider()
    step = _make_step(
        prompt="https://prompt.example/p.mp3",
        params={"audio_url": "https://params.example/p.mp3"},
    )
    assert provider._resolve_audio_url(step) == "https://params.example/p.mp3"


def test_audio_url_falls_back_to_prompt():
    provider = _make_provider()
    step = _make_step(prompt="https://prompt.example/p.mp3")
    assert provider._resolve_audio_url(step) == "https://prompt.example/p.mp3"


def test_empty_input_url_falls_through_to_params():
    # An input asset with an empty url must not short-circuit the precedence
    # chain — it degrades to params["audio_url"] rather than failing the SSRF
    # validator on "".
    provider = _make_provider()
    step = _make_step(
        prompt=None,
        params={"audio_url": "https://params.example/p.mp3"},
        inputs=[Asset(url="", media_type="audio/mpeg")],
    )
    assert provider._resolve_audio_url(step) == "https://params.example/p.mp3"


def test_missing_audio_url_raises_invalid_input():
    provider = _make_provider()
    step = _make_step(prompt=None)
    with pytest.raises(ProviderError) as ei:
        provider.submit(step)
    assert ei.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_unsafe_audio_url_rejected():
    provider = _make_provider()
    step = _make_step(prompt="http://insecure.example/a.mp3")
    with pytest.raises(ProviderError):
        provider.submit(step)


# --- error status ---------------------------------------------------------


def test_error_status_raises_provider_error():
    provider = _make_provider(_fake_transcript(status="error", error="bad audio file"))
    step = _make_step()
    pred_id = provider.submit(step)
    with pytest.raises(ProviderError, match="bad audio file"):
        provider.fetch_output(pred_id, step)


def test_poll_false_until_terminal():
    provider = _make_provider(_fake_transcript(status="processing"))
    step = _make_step()
    pred_id = provider.submit(step)
    assert provider.poll(pred_id) is False


def test_fetch_output_non_terminal_raises():
    # Defensive guard: fetch_output on a still-running transcript must raise
    # rather than emit a silently-empty (text:"") asset and report SUCCEEDED.
    provider = _make_provider(_fake_transcript(status="processing", text=None))
    step = _make_step()
    pred_id = provider.submit(step)
    with pytest.raises(ProviderError, match="not complete") as ei:
        provider.fetch_output(pred_id, step)
    assert ei.value.error_code == ProviderErrorCode.SERVER_ERROR


def test_submit_api_error_wrapped_with_code():
    from genblaze_assemblyai import AssemblyAIProvider

    class _RaisingTranscriber:
        def submit(self, audio_url, config=None):
            err = RuntimeError("boom")
            err.status_code = 429  # type: ignore[attr-defined]
            raise err

    fake = _make_fake_aai()
    fake.Transcriber = _RaisingTranscriber
    provider = AssemblyAIProvider(api_key="test-key", poll_interval=0.0)
    provider._client = fake
    step = _make_step()
    with pytest.raises(ProviderError, match="AssemblyAI submit failed") as ei:
        provider.submit(step)
    assert ei.value.error_code == ProviderErrorCode.RATE_LIMIT


# --- normalize_params -----------------------------------------------------


def test_normalize_params_language_alias():
    provider = _make_provider()
    out = provider.normalize_params({"language": "es", "speaker_labels": True})
    assert out["language_code"] == "es"
    assert "language" not in out
    assert out["speaker_labels"] is True


def test_normalize_params_idempotent():
    provider = _make_provider()
    params = {"language": "es", "speaker_labels": True}
    once = provider.normalize_params(params)
    twice = provider.normalize_params(once)
    assert once == twice


def test_normalize_params_respects_existing_language_code():
    provider = _make_provider()
    out = provider.normalize_params({"language": "es", "language_code": "en_us"})
    # Existing native key wins; the alias is left intact (idempotency guard).
    assert out["language_code"] == "en_us"


# --- credentials ----------------------------------------------------------


def test_missing_api_key_raises_auth_failure(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    from genblaze_assemblyai import AssemblyAIProvider

    provider = AssemblyAIProvider(api_key=None)  # no injected client
    step = _make_step()
    with pytest.raises(ProviderError, match="No AssemblyAI API key") as ei:
        provider.submit(step)
    assert ei.value.error_code == ProviderErrorCode.AUTH_FAILURE


# --- error mapping --------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, ProviderErrorCode.RATE_LIMIT),
        (401, ProviderErrorCode.AUTH_FAILURE),
        (403, ProviderErrorCode.AUTH_FAILURE),
        (400, ProviderErrorCode.INVALID_INPUT),
        (422, ProviderErrorCode.INVALID_INPUT),
        (500, ProviderErrorCode.SERVER_ERROR),
        (503, ProviderErrorCode.SERVER_ERROR),
    ],
)
def test_map_assemblyai_error_status_codes(status, expected):
    from genblaze_assemblyai._errors import map_assemblyai_error

    exc = RuntimeError("err")
    exc.status_code = status  # type: ignore[attr-defined]
    assert map_assemblyai_error(exc) == expected


def test_map_assemblyai_error_no_status_falls_back():
    from genblaze_assemblyai._errors import map_assemblyai_error

    assert map_assemblyai_error(RuntimeError("totally opaque")) == ProviderErrorCode.UNKNOWN


def test_map_assemblyai_error_accepts_error_string():
    from genblaze_assemblyai._errors import map_assemblyai_error

    # transcript.error is a plain string; the classifier handles it.
    assert map_assemblyai_error("rate limit exceeded") == ProviderErrorCode.RATE_LIMIT


# --- catalog decoupling ---------------------------------------------------


def test_declares_discovery_support_none():
    from genblaze_assemblyai import AssemblyAIProvider
    from genblaze_core.providers import DiscoverySupport

    assert AssemblyAIProvider.discovery_support is DiscoverySupport.NONE


@pytest.mark.parametrize("slug", ["universal", "universal-2", "universal-3-pro"])
def test_speech_slug_matches_family(slug):
    from genblaze_core.providers import ValidationOutcome

    provider = _make_provider()
    assert provider.validate_model(slug).outcome is ValidationOutcome.OK_PROVISIONAL


def test_non_speech_slug_unknown_permissive():
    from genblaze_core.providers import ValidationOutcome

    provider = _make_provider()
    assert provider.validate_model("some-other-model").outcome is (
        ValidationOutcome.UNKNOWN_PERMISSIVE
    )


def test_family_and_fallback_carry_no_pricing():
    provider = _make_provider()
    assert provider._models.get("universal-3-pro").pricing is None  # family-matched
    assert provider._models.get("anything-else").pricing is None  # permissive fallback


def test_capabilities_text_only():
    provider = _make_provider()
    caps = provider.get_capabilities()
    assert caps.supported_modalities == [Modality.TEXT]
    assert caps.accepts_chain_input is True
    assert caps.output_formats == ["text/plain"]


# --- compliance harness ---------------------------------------------------


class TestAssemblyAICompliance(ProviderComplianceTests):
    """Verify AssemblyAIProvider satisfies the genblaze provider contract."""

    # AssemblyAI ships zero hardcoded prices (per-minute-of-input-audio is
    # user-registered; see docs/reference/pricing-recipes.md). cost_usd stays
    # None unless the user registers a strategy — same posture as Hume.
    expects_cost = False

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        # Safety net so the lazy ``import assemblyai`` resolves to the fake if
        # ever reached; make_provider also injects ``_client`` directly.
        with patch.dict(sys.modules, {"assemblyai": _make_fake_aai()}):
            yield

    def make_provider(self):
        return _make_provider()

    def make_step(self):
        return _make_step()

    def test_assets_have_valid_urls(self) -> None:
        """Transcripts emit a synthetic ``text:{sha256}`` asset (the
        NvidiaChatProvider TEXT-asset precedent), not an https:// / file://
        URL — so we assert that scheme instead of the harness default."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        assert result.assets
        for asset in result.assets:
            assert asset.url.startswith("text:"), f"Expected text: asset URL, got {asset.url}"
