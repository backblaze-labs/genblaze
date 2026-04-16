"""Tests for LumaProvider (mocked — no real API calls)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_luma():
    """Patch lumaai with a mock client."""
    mock_client = MagicMock()
    mock_client.generations.create.return_value = SimpleNamespace(id="gen-abc")
    mock_client.generations.get.return_value = SimpleNamespace(
        id="gen-abc",
        state="completed",
        assets=SimpleNamespace(video="https://luma-output.com/video.mp4"),
    )

    with patch.dict("sys.modules", {"lumaai": MagicMock()}):
        from genblaze_luma import LumaProvider

        provider = LumaProvider(auth_token="test-key")
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_generation_id(mock_luma):
    provider, client = mock_luma
    step = Step(provider="luma", model="ray-2", prompt="a sunset over ocean")
    gen_id = provider.submit(step)
    assert gen_id == "gen-abc"
    client.generations.create.assert_called_once()


def test_poll_returns_true_on_completed(mock_luma):
    provider, _ = mock_luma
    assert provider.poll("gen-abc") is True


def test_poll_returns_false_on_dreaming(mock_luma):
    provider, client = mock_luma
    client.generations.get.return_value = SimpleNamespace(id="gen-abc", state="dreaming")
    assert provider.poll("gen-abc") is False


def test_fetch_output_attaches_asset(mock_luma):
    provider, _ = mock_luma
    step = Step(provider="luma", model="ray-2", prompt="a sunset")
    result = provider.fetch_output("gen-abc", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    assert "luma-output.com" in result.assets[0].url


def test_fetch_output_failed_raises(mock_luma):
    provider, client = mock_luma
    client.generations.get.return_value = SimpleNamespace(
        id="gen-abc", state="failed", failure_reason="Content policy violation"
    )
    step = Step(provider="luma", model="ray-2", prompt="bad")
    with pytest.raises(ProviderError, match="Content policy violation"):
        provider.fetch_output("gen-abc", step)


def test_invoke_full_lifecycle(mock_luma):
    provider, _ = mock_luma
    step = Step(provider="luma", model="ray-2", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_aspect_ratio_raises(mock_luma):
    provider, _ = mock_luma
    step = Step(
        provider="luma",
        model="ray-2",
        prompt="test",
        params={"aspect_ratio": "5:3"},
    )
    with pytest.raises(ProviderError, match="Invalid aspect_ratio"):
        provider.submit(step)


def test_cost_tracked(mock_luma):
    """Cost is set based on model tier."""
    provider, _ = mock_luma
    step = Step(provider="luma", model="ray-2", prompt="a sunset")
    result = provider.fetch_output("gen-abc", step)
    assert result.cost_usd is not None
    assert result.cost_usd == 0.40


def test_cost_tracked_flash(mock_luma):
    """Flash model has lower cost."""
    provider, _ = mock_luma
    step = Step(provider="luma", model="ray-flash-2", prompt="a sunset")
    result = provider.fetch_output("gen-abc", step)
    assert result.cost_usd == 0.20


def test_cost_none_unknown_model(mock_luma):
    """Cost stays None for unknown model."""
    provider, _ = mock_luma
    step = Step(provider="luma", model="unknown-model", prompt="a sunset")
    result = provider.fetch_output("gen-abc", step)
    assert result.cost_usd is None


def test_duration_param_forwarded(mock_luma):
    """Standard 'duration' param is forwarded to Luma API."""
    provider, client = mock_luma
    step = Step(
        provider="luma",
        model="ray-2",
        prompt="test",
        params={"duration": "5s"},
    )
    provider.submit(step)
    call_kwargs = client.generations.create.call_args[1]
    assert call_kwargs["duration"] == "5s"


# --- Compliance harness ---


class TestLumaCompliance(ProviderComplianceTests):
    """Verify LumaProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"lumaai": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_luma import LumaProvider

        mock_client = MagicMock()
        mock_client.generations.create.return_value = SimpleNamespace(id="gen-abc")
        mock_client.generations.get.return_value = SimpleNamespace(
            id="gen-abc",
            state="completed",
            assets=SimpleNamespace(video="https://luma-output.com/video.mp4"),
        )
        provider = LumaProvider(auth_token="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="luma", model="ray-2", prompt="test prompt")
