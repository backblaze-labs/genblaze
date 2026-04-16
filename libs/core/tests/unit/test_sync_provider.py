"""Tests for SyncProvider, validate_asset_url, adaptive polling,
SubmitResult, and ProviderCapabilities."""

from __future__ import annotations

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    ProviderCapabilities,
    SubmitResult,
    SyncProvider,
    _adaptive_poll_interval,
    validate_asset_url,
)
from genblaze_core.runnable.config import RunnableConfig
from genblaze_core.testing import ProviderComplianceTests


class StubSyncProvider(SyncProvider):
    """Minimal sync provider for testing."""

    name = "stub-sync"

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
        return step


class FailingSyncProvider(SyncProvider):
    """Sync provider that always raises."""

    name = "fail-sync"

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        raise ProviderError("generation failed")


# --- SyncProvider lifecycle ---


def test_sync_provider_invoke():
    """SyncProvider.invoke() runs the full lifecycle via generate()."""
    provider = StubSyncProvider()
    step = Step(provider="stub-sync", model="m", prompt="hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1
    assert result.assets[0].url == "https://example.com/out.png"


def test_sync_provider_failure():
    """Failures in generate() are handled by the base invoke() retry logic."""
    provider = FailingSyncProvider()
    step = Step(provider="fail-sync", model="m", prompt="hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.FAILED
    assert "generation failed" in result.error


@pytest.mark.asyncio
async def test_sync_provider_ainvoke():
    """SyncProvider works with async pipeline via ainvoke()."""
    provider = StubSyncProvider()
    step = Step(provider="stub-sync", model="m", prompt="hello")
    result = await provider.ainvoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_sync_provider_thread_safety():
    """Concurrent invocations don't share state via self."""
    provider = StubSyncProvider()
    # Run two sequential invocations — each should get independent results
    step1 = Step(provider="stub-sync", model="m", prompt="a")
    step2 = Step(provider="stub-sync", model="m", prompt="b")
    r1 = provider.invoke(step1)
    r2 = provider.invoke(step2)
    assert r1.step_id != r2.step_id
    assert len(r1.assets) == 1
    assert len(r2.assets) == 1


def test_sync_provider_in_pipeline():
    """SyncProvider works as a drop-in for Pipeline.step()."""
    from genblaze_core.pipeline import Pipeline

    provider = StubSyncProvider()
    result = Pipeline("sync-test").step(provider, model="m", prompt="hello").run()
    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert result.manifest.verify()


def test_sync_provider_retry_clears_stale_result():
    """On retry, stale results from a failed attempt must not leak."""
    from unittest.mock import patch

    call_count = 0

    class RetryableSyncProvider(SyncProvider):
        name = "retryable-sync"

        def generate(self, step, config=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProviderError("server error 500")
            step.assets.append(Asset(url="https://example.com/retry.png", media_type="image/png"))
            return step

    provider = RetryableSyncProvider()
    step = Step(provider="retryable-sync", model="m", prompt="hello")
    with patch("genblaze_core.providers.base.time.sleep"):
        result = provider.invoke(step, {"max_retries": 2})
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1
    # No leaked entries in the provider's result cache
    assert len(provider._sync_results) == 0


# --- validate_asset_url ---


def test_validate_asset_url_https():
    """HTTPS URLs are accepted."""
    validate_asset_url("https://example.com/image.png")


def test_validate_asset_url_rejects_http():
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        validate_asset_url("http://example.com/image.png")


def test_validate_asset_url_rejects_file():
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        validate_asset_url("file:///etc/passwd")


def test_validate_asset_url_rejects_empty():
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        validate_asset_url("")


# --- Compliance test harness ---


class TestStubCompliance(ProviderComplianceTests):
    """Verify the compliance harness works with our stub provider."""

    def make_provider(self):
        return StubSyncProvider()


# --- Adaptive polling ---


def test_adaptive_poll_interval_starts_at_base():
    """At elapsed=0, adaptive interval equals the base."""
    assert _adaptive_poll_interval(0.0, base=2.0) == 2.0


def test_adaptive_poll_interval_increases():
    """After 30s, interval doubles."""
    assert _adaptive_poll_interval(30.0, base=2.0) == 4.0
    assert _adaptive_poll_interval(60.0, base=2.0) == 8.0


def test_adaptive_poll_interval_capped():
    """Interval never exceeds max_interval."""
    assert _adaptive_poll_interval(300.0, base=2.0, max_interval=15.0) == 15.0


# --- SubmitResult ---


def test_submit_result_backward_compat():
    """Providers returning a plain string still work with the new polling logic."""
    from typing import Any

    from genblaze_core.providers.base import BaseProvider

    class PlainProvider(BaseProvider):
        name = "plain"

        def submit(self, step, config=None) -> Any:
            return "pred-abc"  # Plain string, not SubmitResult

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step) -> Step:
            step.assets.append(Asset(url="https://example.com/o.png", media_type="image/png"))
            return step

    provider = PlainProvider()
    step = Step(provider="plain", model="m", prompt="p")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED


def test_base_provider_normalize_noop():
    """Default normalize_params returns params unchanged."""
    from genblaze_core.providers.base import BaseProvider

    class NoopProvider(BaseProvider):
        name = "noop"

        def submit(self, step, config=None):
            return "x"

        def poll(self, pid, config=None):
            return True

        def fetch_output(self, pid, step):
            return step

    p = NoopProvider()
    params = {"duration": 10, "resolution": "1080p"}
    assert p.normalize_params(params) == params


def test_normalize_called_in_pipeline_build_step():
    """Pipeline._build_step calls provider.normalize_params before creating the Step."""
    from genblaze_core.pipeline.pipeline import Pipeline

    class MappingProvider(StubSyncProvider):
        name = "mapping"

        def normalize_params(self, params, modality=None):
            p = dict(params)
            if "duration" in p:
                p["seconds"] = p.pop("duration")
            return p

    provider = MappingProvider()
    result = Pipeline("norm-test").step(provider, model="m", prompt="p", duration=10).run()

    # The Step in the manifest should have normalized params
    step = result.run.steps[0]
    assert "seconds" in step.params
    assert "duration" not in step.params
    assert step.params["seconds"] == 10


def test_submit_result_with_estimated_seconds():
    """SubmitResult.estimated_seconds delays the first poll."""
    from typing import Any
    from unittest.mock import patch

    from genblaze_core.providers.base import BaseProvider

    sleeps: list[float] = []

    class HintProvider(BaseProvider):
        name = "hint"

        def submit(self, step, config=None) -> Any:
            return SubmitResult(prediction_id="pred-1", estimated_seconds=10.0)

        def poll(self, prediction_id, config=None) -> bool:
            return True

        def fetch_output(self, prediction_id, step) -> Step:
            step.assets.append(Asset(url="https://example.com/o.mp4", media_type="video/mp4"))
            return step

    provider = HintProvider()
    step = Step(provider="hint", model="m", prompt="p")

    with patch("genblaze_core.providers.base.time.sleep", side_effect=lambda s: sleeps.append(s)):
        result = provider.invoke(step, {"timeout": 300})

    assert result.status == StepStatus.SUCCEEDED
    # Should have delayed first poll by ~80% of 10s = 8s
    assert len(sleeps) == 1
    assert 7.5 <= sleeps[0] <= 8.5


# --- ProviderCapabilities ---


def test_provider_capabilities_defaults_to_none():
    """All ProviderCapabilities fields default to None when unspecified."""
    caps = ProviderCapabilities()
    assert caps.supported_modalities is None
    assert caps.supported_inputs is None
    assert caps.max_duration is None
    assert caps.resolutions is None
    assert caps.output_formats is None
    assert caps.models is None


def test_base_provider_get_capabilities_returns_none():
    """BaseProvider.get_capabilities() returns None by default."""
    from genblaze_core.providers.base import BaseProvider

    class MinimalProvider(BaseProvider):
        name = "minimal"

        def submit(self, step, config=None):
            return "x"

        def poll(self, pid, config=None):
            return True

        def fetch_output(self, pid, step):
            return step

    p = MinimalProvider()
    assert p.get_capabilities() is None


def test_provider_with_capabilities():
    """A provider implementing get_capabilities() returns the correct dataclass."""

    class VideoProvider(StubSyncProvider):
        name = "video-stub"

        def get_capabilities(self):
            return ProviderCapabilities(
                supported_modalities=[Modality.VIDEO],
                supported_inputs=["text", "image"],
                max_duration=60.0,
                resolutions=["720p", "1080p"],
                output_formats=["video/mp4"],
                models=["model-a", "model-b"],
            )

    p = VideoProvider()
    caps = p.get_capabilities()
    assert isinstance(caps, ProviderCapabilities)
    assert caps.supported_modalities == [Modality.VIDEO]
    assert caps.supported_inputs == ["text", "image"]
    assert caps.max_duration == 60.0
    assert caps.resolutions == ["720p", "1080p"]
    assert caps.output_formats == ["video/mp4"]
    assert caps.models == ["model-a", "model-b"]


def test_provider_capabilities_importable_from_top_level():
    """ProviderCapabilities is accessible via the genblaze_core top-level import."""
    from genblaze_core import ProviderCapabilities as PC

    assert PC is ProviderCapabilities
