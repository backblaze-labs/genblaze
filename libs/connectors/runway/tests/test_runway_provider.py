"""Tests for RunwayProvider (mocked — no real API calls)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_runway():
    """Patch runwayml with a mock client."""
    mock_client = MagicMock()
    mock_client.image_to_video.create.return_value = SimpleNamespace(id="task-abc")
    mock_client.tasks.retrieve.return_value = SimpleNamespace(
        id="task-abc",
        status="SUCCEEDED",
        output=["https://runway-output.com/video.mp4"],
    )

    with patch.dict("sys.modules", {"runwayml": MagicMock()}):
        from genblaze_runway import RunwayProvider

        provider = RunwayProvider(api_secret="test-key")
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_task_id(mock_runway):
    provider, client = mock_runway
    step = Step(provider="runway", model="gen4_turbo", prompt="a sunset")
    task_id = provider.submit(step)
    assert task_id == "task-abc"
    client.image_to_video.create.assert_called_once()


def test_poll_returns_true_on_succeeded(mock_runway):
    provider, _ = mock_runway
    assert provider.poll("task-abc") is True


def test_poll_returns_false_on_running(mock_runway):
    provider, client = mock_runway
    client.tasks.retrieve.return_value = SimpleNamespace(id="task-abc", status="RUNNING")
    assert provider.poll("task-abc") is False


def test_fetch_output_attaches_asset(mock_runway):
    provider, _ = mock_runway
    step = Step(provider="runway", model="gen4_turbo", prompt="a sunset")
    result = provider.fetch_output("task-abc", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    assert "runway-output.com" in result.assets[0].url


def test_fetch_output_failed_raises(mock_runway):
    provider, client = mock_runway
    client.tasks.retrieve.return_value = SimpleNamespace(
        id="task-abc", status="FAILED", failure="Content moderation"
    )
    step = Step(provider="runway", model="gen4_turbo", prompt="bad")
    with pytest.raises(ProviderError, match="Content moderation"):
        provider.fetch_output("task-abc", step)


def test_invoke_full_lifecycle(mock_runway):
    provider, _ = mock_runway
    step = Step(provider="runway", model="gen4_turbo", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_duration_raises(mock_runway):
    provider, _ = mock_runway
    step = Step(provider="runway", model="gen4_turbo", prompt="test", params={"duration": 7})
    with pytest.raises(ProviderError, match="Invalid duration"):
        provider.submit(step)


def test_cost_tracked(mock_runway):
    """Cost is set based on model and duration."""
    provider, _ = mock_runway
    step = Step(provider="runway", model="gen4_turbo", prompt="a sunset", params={"duration": 10})
    result = provider.fetch_output("task-abc", step)
    assert result.cost_usd is not None
    assert result.cost_usd == 1.00


def test_cost_tracked_default_duration(mock_runway):
    """Default 5s duration uses 5s price."""
    provider, _ = mock_runway
    step = Step(provider="runway", model="gen4_turbo", prompt="a sunset")
    result = provider.fetch_output("task-abc", step)
    assert result.cost_usd == 0.50


def test_cost_none_unknown_model(mock_runway):
    """Cost stays None for unknown model."""
    provider, _ = mock_runway
    step = Step(provider="runway", model="unknown-model", prompt="a sunset")
    result = provider.fetch_output("task-abc", step)
    assert result.cost_usd is None


def test_aspect_ratio_alias(mock_runway):
    """Standard 'aspect_ratio' param is mapped to 'ratio' via normalize_params."""
    provider, client = mock_runway
    # normalize_params maps aspect_ratio → ratio
    params = provider.normalize_params({"aspect_ratio": "16:9"})
    step = Step(
        provider="runway",
        model="gen4_turbo",
        prompt="test",
        params=params,
    )
    provider.submit(step)
    call_kwargs = client.image_to_video.create.call_args[1]
    assert call_kwargs["ratio"] == "16:9"


def test_invalid_aspect_ratio_alias_raises(mock_runway):
    """Invalid aspect_ratio via alias is validated after normalization."""
    provider, _ = mock_runway
    # normalize_params maps aspect_ratio → ratio
    params = provider.normalize_params({"aspect_ratio": "4:3"})
    step = Step(
        provider="runway",
        model="gen4_turbo",
        prompt="test",
        params=params,
    )
    with pytest.raises(ProviderError, match="Invalid ratio"):
        provider.submit(step)


# --- Compliance harness ---


def test_poll_progress_surfaces_preview_and_progress(mock_runway):
    """poll_progress() reads the cached in-progress task without a 2nd API call."""
    provider, client = mock_runway
    # First poll returns a RUNNING task with progress + a preview thumbnail
    client.tasks.retrieve.return_value = SimpleNamespace(
        id="task-abc",
        status="RUNNING",
        progress=0.42,
        thumbnail_url="https://runway-preview.test/frame-0042.jpg",
    )
    assert provider.poll("task-abc") is False
    signals = provider.poll_progress("task-abc")
    assert signals is not None
    assert signals["progress_pct"] == 0.42
    assert signals["preview_url"] == "https://runway-preview.test/frame-0042.jpg"


def test_poll_progress_returns_none_before_first_poll(mock_runway):
    """poll_progress() before any poll() call returns None (nothing cached)."""
    provider, _ = mock_runway
    assert provider.poll_progress("task-never-polled") is None


def test_poll_progress_omits_missing_fields(mock_runway):
    """When the SDK doesn't expose progress/thumbnail, return None (not an empty dict)."""
    provider, client = mock_runway
    client.tasks.retrieve.return_value = SimpleNamespace(id="task-abc", status="RUNNING")
    assert provider.poll("task-abc") is False
    assert provider.poll_progress("task-abc") is None


class TestRunwayCompliance(ProviderComplianceTests):
    """Verify RunwayProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"runwayml": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_runway import RunwayProvider

        mock_client = MagicMock()
        mock_client.image_to_video.create.return_value = SimpleNamespace(id="task-abc")
        mock_client.tasks.retrieve.return_value = SimpleNamespace(
            id="task-abc",
            status="SUCCEEDED",
            output=["https://runway-output.com/video.mp4"],
        )
        provider = RunwayProvider(api_secret="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="runway", model="gen4_turbo", prompt="test prompt")
