"""Pytest-free mock providers for pipeline testing.

These are the runtime-importable equivalents of the test helpers in
``genblaze_core.testing``. They have *no* pytest dependency, so they
can be imported in production code and non-pytest test harnesses alike.

``genblaze_core.testing`` re-exports all three classes for backward
compatibility; callers can use either import path.

Example::

    from genblaze_core.mocks import MockVideoProvider
    from genblaze_core.pipeline import Pipeline

    result = Pipeline("test").step(MockVideoProvider(), model="mock", prompt="a cat").run()
    assert result.run.steps[0].assets[0].media_type == "video/mp4"
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, VideoMetadata
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SyncProvider
from genblaze_core.runnable.config import RunnableConfig


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
        return [
            Asset(
                url="https://mock.test/output.bin",
                media_type="application/octet-stream",
                sha256="0" * 64,
            )
        ]

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
        asset = Asset(url="https://mock.test/video.mp4", media_type="video/mp4", sha256="1" * 64)
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
        asset = Asset(url="https://mock.test/audio.mp3", media_type="audio/mpeg", sha256="2" * 64)
        asset.audio = AudioMetadata(codec="mp3", channels=1, sample_rate=44100)
        return [asset]
