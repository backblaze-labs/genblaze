"""Tests for MockProvider, MockVideoProvider, and MockAudioProvider."""

import time

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import ProviderErrorCode, RunStatus, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.pipeline import Pipeline
from genblaze_core.testing import MockAudioProvider, MockProvider, MockVideoProvider

# --- MockProvider basics ---


def test_mock_provider_returns_default_asset() -> None:
    """MockProvider returns a single default asset."""
    provider = MockProvider()
    step = Step(provider="mock", model="test", prompt="hello")
    result = provider.invoke(step)

    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "application/octet-stream"


def test_mock_provider_returns_configured_assets() -> None:
    """MockProvider returns user-supplied asset list."""
    custom = [Asset(url="https://mock.test/custom.wav", media_type="audio/wav")]
    provider = MockProvider(assets=custom)
    step = Step(provider="mock", model="m", prompt="p")
    result = provider.invoke(step)

    assert len(result.assets) == 1
    assert result.assets[0].url == "https://mock.test/custom.wav"


def test_mock_provider_asset_factory() -> None:
    """MockProvider accepts a callable factory for dynamic assets."""

    def factory(s: Step) -> list[Asset]:
        return [Asset(url=f"https://mock.test/{s.prompt}.png", media_type="image/png")]

    provider = MockProvider(assets=factory)
    step = Step(provider="mock", model="m", prompt="cat")
    result = provider.invoke(step)

    assert result.assets[0].url == "https://mock.test/cat.png"


def test_mock_provider_tracks_calls() -> None:
    """MockProvider records call_count and received steps."""
    provider = MockProvider()
    for i in range(3):
        provider.invoke(Step(provider="mock", model="m", prompt=f"p{i}"))

    assert provider.call_count == 3
    assert len(provider.received_steps) == 3
    assert provider.received_steps[1].prompt == "p1"


# --- MockVideoProvider ---


def test_mock_video_provider_defaults() -> None:
    """MockVideoProvider returns video/mp4 with VideoMetadata."""
    provider = MockVideoProvider()
    step = Step(provider="mock-video", model="m", prompt="p")
    result = provider.invoke(step)

    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "video/mp4"
    assert asset.video is not None
    assert asset.video.codec == "h264"
    assert asset.video.has_audio is False


def test_mock_video_provider_custom_name() -> None:
    """MockVideoProvider accepts custom name."""
    provider = MockVideoProvider(name="my-video")
    assert provider.name == "my-video"


# --- MockAudioProvider ---


def test_mock_audio_provider_defaults() -> None:
    """MockAudioProvider returns audio/mpeg with AudioMetadata."""
    provider = MockAudioProvider()
    step = Step(provider="mock-audio", model="m", prompt="p")
    result = provider.invoke(step)

    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "audio/mpeg"
    assert asset.audio is not None
    assert asset.audio.codec == "mp3"
    assert asset.audio.channels == 1
    assert asset.audio.sample_rate == 44100


# --- Latency simulation ---


def test_mock_provider_latency() -> None:
    """MockProvider with latency takes at least that long."""
    provider = MockProvider(latency=0.1)
    step = Step(provider="mock", model="m", prompt="p")

    start = time.monotonic()
    provider.invoke(step)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1


# --- Failure mode ---


def test_mock_provider_should_fail() -> None:
    """MockProvider with should_fail=True produces FAILED step."""
    provider = MockProvider(
        should_fail=True,
        error_code=ProviderErrorCode.AUTH_FAILURE,
        error_message="bad key",
    )
    step = Step(provider="mock", model="m", prompt="p")
    result = provider.invoke(step)

    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.AUTH_FAILURE
    assert "bad key" in result.error


def test_mock_provider_should_fail_default_code() -> None:
    """MockProvider default error_code is UNKNOWN."""
    provider = MockProvider(should_fail=True)
    step = Step(provider="mock", model="m", prompt="p")
    result = provider.invoke(step)

    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.UNKNOWN


# --- Cost tracking ---


def test_mock_provider_cost_usd() -> None:
    """MockProvider populates cost_usd on the step."""
    provider = MockProvider(cost_usd=0.05)
    step = Step(provider="mock", model="m", prompt="p")
    result = provider.invoke(step)

    assert result.status == StepStatus.SUCCEEDED
    assert result.cost_usd == 0.05


def test_mock_provider_no_cost_by_default() -> None:
    """Without cost_usd, step.cost_usd stays None."""
    provider = MockProvider()
    step = Step(provider="mock", model="m", prompt="p")
    result = provider.invoke(step)

    assert result.cost_usd is None


# --- Pipeline integration ---


def test_mock_provider_in_pipeline() -> None:
    """MockProvider works end-to-end in a Pipeline."""
    provider = MockVideoProvider()
    result = Pipeline("mock-test").step(provider, model="m", prompt="test video").run()

    assert result.run.status == RunStatus.COMPLETED
    assert len(result.run.steps) == 1
    assert result.run.steps[0].assets[0].media_type == "video/mp4"
    assert result.manifest.verify()


def test_mock_providers_multi_step_pipeline() -> None:
    """Video + audio mock providers in a chained pipeline."""
    video = MockVideoProvider()
    audio = MockAudioProvider()

    result = (
        Pipeline("av-test")
        .step(video, model="v-model", prompt="a sunset")
        .step(audio, model="a-model", prompt="ocean waves")
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert result.run.steps[0].assets[0].media_type == "video/mp4"
    assert result.run.steps[1].assets[0].media_type == "audio/mpeg"


def test_mock_provider_failure_in_pipeline() -> None:
    """Failed mock provider triggers pipeline failure with fail_fast."""
    provider = MockProvider(should_fail=True, error_code=ProviderErrorCode.SERVER_ERROR)
    result = Pipeline("fail-test").step(provider, model="m", prompt="p").run()

    assert result.run.status == RunStatus.FAILED


# --- Lazy import from genblaze_core ---


def test_lazy_import() -> None:
    """Mock providers are importable from genblaze_core top-level."""
    import genblaze_core

    assert genblaze_core.MockProvider is MockProvider
    assert genblaze_core.MockVideoProvider is MockVideoProvider
    assert genblaze_core.MockAudioProvider is MockAudioProvider
