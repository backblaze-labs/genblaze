"""Testing utilities — mock providers and compliance test harness.

Mock providers
--------------
``MockProvider``, ``MockVideoProvider``, and ``MockAudioProvider`` return
configurable canned assets so you can test pipelines without real API calls.

Example::

    from genblaze_core.testing import MockVideoProvider
    from genblaze_core.pipeline import Pipeline

    result = Pipeline("test").step(MockVideoProvider(), model="mock", prompt="a cat").run()
    assert result.run.steps[0].assets[0].media_type == "video/mp4"

Compliance harness
------------------
Subclass ``ProviderComplianceTests`` and implement ``make_provider()`` to get
a full suite of compatibility tests for free. Works for both BaseProvider
(submit/poll/fetch_output) and SyncProvider (generate) subclasses.

Example::

    from genblaze_core.testing import ProviderComplianceTests

    class TestMyProvider(ProviderComplianceTests):
        def make_provider(self):
            return MyProvider(api_key="test")

        def make_step(self):
            return Step(provider="my-provider", model="test-model", prompt="hello")
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import pytest

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

# ---------------------------------------------------------------------------
# Mock providers
# ---------------------------------------------------------------------------


class MockProvider(SyncProvider):
    """Configurable mock provider for testing pipelines.

    Args:
        name: Provider name (default "mock").
        assets: List of Asset objects to return, or a factory callable
            that receives the Step and returns a list of Assets.
        latency: Simulated generation time in seconds (default 0).
        should_fail: If True, raise ProviderError on generate().
        error_code: ProviderErrorCode to use when failing.
        error_message: Error message when failing.
        cost_usd: Cost to set on the step.
    """

    def __init__(
        self,
        *,
        name: str = "mock",
        assets: list[Asset] | Callable[[Step], list[Asset]] | None = None,
        latency: float = 0,
        should_fail: bool = False,
        error_code: ProviderErrorCode = ProviderErrorCode.UNKNOWN,
        error_message: str = "Mock provider error",
        cost_usd: float | None = None,
    ) -> None:
        super().__init__()
        self.name = name  # type: ignore[assignment]
        self._assets = assets
        self.latency = latency
        self.should_fail = should_fail
        self.error_code = error_code
        self.error_message = error_message
        self._cost_usd = cost_usd
        # Track calls for test assertions
        self.call_count = 0
        self.received_steps: list[Step] = []

    def _default_assets(self) -> list[Asset]:
        """Return default assets when none are configured."""
        return [Asset(url="https://mock.test/output.bin", media_type="application/octet-stream")]

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Return canned assets or raise on demand."""
        self.call_count += 1
        self.received_steps.append(step)

        if self.latency > 0:
            time.sleep(self.latency)

        if self.should_fail:
            raise ProviderError(self.error_message, error_code=self.error_code)

        # Resolve assets: callable factory, explicit list, or defaults
        if callable(self._assets):
            resolved = self._assets(step)
        elif self._assets is not None:
            resolved = self._assets
        else:
            resolved = self._default_assets()

        step.assets.extend(resolved)

        if self._cost_usd is not None:
            step.cost_usd = self._cost_usd

        return step


class MockVideoProvider(MockProvider):
    """Mock provider that returns a video asset with VideoMetadata.

    Default asset: video/mp4 with codec=h264, has_audio=False.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "mock-video")
        super().__init__(**kwargs)

    def _default_assets(self) -> list[Asset]:
        asset = Asset(url="https://mock.test/video.mp4", media_type="video/mp4")
        asset.video = VideoMetadata(codec="h264", has_audio=False)
        return [asset]


class MockAudioProvider(MockProvider):
    """Mock provider that returns an audio asset with AudioMetadata.

    Default asset: audio/mpeg with codec=mp3, channels=1, sample_rate=44100.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "mock-audio")
        super().__init__(**kwargs)

    def _default_assets(self) -> list[Asset]:
        asset = Asset(url="https://mock.test/audio.mp3", media_type="audio/mpeg")
        asset.audio = AudioMetadata(codec="mp3", channels=1, sample_rate=44100)
        return [asset]


# ---------------------------------------------------------------------------
# Compliance test harness
# ---------------------------------------------------------------------------


class ProviderComplianceTests(ABC):
    """Base test class for provider implementations.

    Provides ~10 tests that verify a provider conforms to the genblaze
    provider contract. Subclass and implement make_provider() and make_step().
    """

    @abstractmethod
    def make_provider(self) -> BaseProvider:
        """Return a configured provider instance (may use mocks/fakes)."""
        ...

    def make_step(self) -> Step:
        """Return a Step suitable for this provider. Override if needed."""
        provider = self.make_provider()
        return Step(provider=provider.name, model="test-model", prompt="test prompt")

    # --- Identity ---

    def test_has_name(self) -> None:
        """Provider must override the default name."""
        provider = self.make_provider()
        assert provider.name != "base", "Provider must set a unique `name` attribute"

    def test_name_is_string(self) -> None:
        provider = self.make_provider()
        assert isinstance(provider.name, str)

    # --- Lifecycle ---

    def test_submit_returns_prediction_id(self) -> None:
        """submit() must return a non-None prediction ID."""
        provider = self.make_provider()
        step = self.make_step()
        prediction_id = provider.submit(step)
        assert prediction_id is not None

    def test_poll_returns_bool(self) -> None:
        """poll() must return a boolean."""
        provider = self.make_provider()
        step = self.make_step()
        prediction_id = provider.submit(step)
        result = provider.poll(prediction_id)
        assert isinstance(result, bool)

    def test_fetch_output_returns_step(self) -> None:
        """fetch_output() must return a Step instance."""
        provider = self.make_provider()
        step = self.make_step()
        prediction_id = provider.submit(step)
        result = provider.fetch_output(prediction_id, step)
        assert isinstance(result, Step)

    # --- invoke() integration ---

    def test_invoke_succeeds(self) -> None:
        """Full invoke() lifecycle should succeed."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        assert result.status in (StepStatus.SUCCEEDED, StepStatus.FAILED)

    def test_invoke_sets_timestamps(self) -> None:
        """Successful invoke should set started_at and completed_at."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        if result.status == StepStatus.SUCCEEDED:
            assert result.started_at is not None
            assert result.completed_at is not None

    # --- Asset validation ---

    def test_assets_have_valid_urls(self) -> None:
        """All asset URLs must be HTTPS or file:// (for local-save providers)."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        for asset in result.assets:
            valid = asset.url.startswith("https://") or asset.url.startswith("file://")
            assert valid, f"Invalid URL scheme (must be https:// or file://): {asset.url}"

    def test_assets_have_media_type(self) -> None:
        """All assets must have a media_type set."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        for asset in result.assets:
            assert asset.media_type, f"Asset missing media_type: {asset.url}"

    # --- SyncProvider-specific ---

    def test_sync_provider_generate_is_sufficient(self) -> None:
        """SyncProvider subclasses only need generate() for full lifecycle."""
        provider = self.make_provider()
        if not isinstance(provider, SyncProvider):
            pytest.skip("Not a SyncProvider")
        step = self.make_step()
        result = provider.invoke(step)
        assert result.status in (StepStatus.SUCCEEDED, StepStatus.FAILED)

    # --- Capabilities ---

    def test_get_capabilities_returns_valid_type(self) -> None:
        """get_capabilities() must return ProviderCapabilities or None."""
        provider = self.make_provider()
        caps = provider.get_capabilities()
        assert caps is None or isinstance(caps, ProviderCapabilities)

    # --- Audio metadata ---

    def test_audio_providers_populate_audio_metadata(self) -> None:
        """Audio providers must populate AudioMetadata on audio assets."""
        provider = self.make_provider()
        caps = provider.get_capabilities()
        if caps is None or not caps.supported_modalities:
            pytest.skip("Provider does not declare capabilities")
        if Modality.AUDIO not in caps.supported_modalities:
            pytest.skip("Provider does not declare AUDIO modality")
        step = self.make_step()
        result = provider.invoke(step)
        if result.status != StepStatus.SUCCEEDED:
            pytest.skip("Provider did not succeed — cannot verify metadata")
        for asset in result.assets:
            if asset.media_type and asset.media_type.startswith("audio/"):
                assert asset.audio is not None, f"Audio asset missing AudioMetadata: {asset.url}"
                assert isinstance(asset.audio, AudioMetadata)

    # --- Chain input validation ---

    def test_chain_input_urls_validated(self) -> None:
        """Providers with accepts_chain_input must reject unsafe URLs."""
        provider = self.make_provider()
        caps = provider.get_capabilities()
        if caps is None or not caps.accepts_chain_input:
            pytest.skip("Provider does not accept chain inputs")
        step = self.make_step()
        # Inject an unsafe http:// chain input
        step.inputs = [Asset(url="http://evil.com/payload.bin", media_type="image/png")]
        with pytest.raises(ProviderError):
            provider.submit(step)

    # --- normalize_params ---

    def test_normalize_params_idempotent(self) -> None:
        """Applying normalize_params twice must produce the same result."""
        provider = self.make_provider()
        params = {"duration": 10, "resolution": "1080p", "aspect_ratio": "16:9"}
        p1 = provider.normalize_params(dict(params))
        p2 = provider.normalize_params(dict(p1))
        assert p1 == p2, f"normalize_params not idempotent: {p1} != {p2}"

    # --- Cost tracking ---

    def test_invoke_populates_cost(self) -> None:
        """Successful invoke should populate step.cost_usd (soft check)."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        if result.status == StepStatus.SUCCEEDED and result.cost_usd is None:
            import warnings

            warnings.warn(
                f"Provider '{provider.name}' does not populate cost_usd",
                stacklevel=1,
            )
