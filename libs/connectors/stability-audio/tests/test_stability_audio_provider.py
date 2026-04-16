"""Tests for StabilityAudioProvider (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_stability(tmp_path):
    """Patch httpx with a mock client."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"fake-audio-bytes"

    mock_http = MagicMock()
    mock_http.post.return_value = mock_response

    with patch.dict("sys.modules", {"httpx": MagicMock()}):
        from genblaze_stability_audio import StabilityAudioProvider

        provider = StabilityAudioProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._http_client = mock_http
        yield provider, mock_http


def test_generate_returns_audio_asset(mock_stability):
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="Upbeat electronic music with synths",
    )
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "audio/mpeg"
    assert result.assets[0].url.startswith("file://")


def test_invoke_full_lifecycle(mock_stability):
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="Ambient soundscape",
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_duration_param_passed(mock_stability):
    provider, http_client = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="test",
        params={"duration": "30"},
    )
    provider.generate(step)
    call_kwargs = http_client.post.call_args
    assert "30" in str(call_kwargs)


def test_invalid_duration_raises(mock_stability):
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="test",
        params={"duration": "200"},
    )
    with pytest.raises(ProviderError, match="Invalid duration"):
        provider.generate(step)


def test_api_error_raises(mock_stability):
    provider, http_client = mock_stability
    error_resp = MagicMock()
    error_resp.status_code = 401
    error_resp.text = "Unauthorized"
    http_client.post.return_value = error_resp
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="test",
    )
    with pytest.raises(ProviderError, match="Stability Audio API error 401"):
        provider.generate(step)


def test_cost_tracked_with_duration(mock_stability):
    """Cost is set based on requested duration."""
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="Upbeat music",
        params={"duration": "30"},
    )
    result = provider.generate(step)
    assert result.cost_usd is not None
    assert result.cost_usd == pytest.approx(30.0 * 0.01)


def test_cost_none_without_duration(mock_stability):
    """Cost stays None when no duration param is set."""
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="Ambient soundscape",
    )
    result = provider.generate(step)
    assert result.cost_usd is None


def test_audio_type_metadata(mock_stability):
    """Stability Audio assets are tagged as music."""
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="Upbeat electronic",
    )
    result = provider.generate(step)
    assert result.assets[0].metadata["audio_type"] == "music"


def test_asset_duration_set(mock_stability):
    """Asset duration is set from params."""
    provider, _ = mock_stability
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="test",
        params={"duration": "30"},
    )
    result = provider.generate(step)
    assert result.assets[0].duration == 30.0


def test_rate_limit_error_code(mock_stability):
    provider, http_client = mock_stability
    error_resp = MagicMock()
    error_resp.status_code = 429
    error_resp.text = "Rate limited"
    http_client.post.return_value = error_resp
    step = Step(
        provider="stability-audio",
        model="stable-audio-2.5",
        prompt="test",
    )
    with pytest.raises(ProviderError) as exc_info:
        provider.generate(step)
    assert exc_info.value.error_code.value == "rate_limit"


# --- Compliance harness ---


class TestStabilityAudioCompliance(ProviderComplianceTests):
    """Verify StabilityAudioProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"httpx": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_stability_audio import StabilityAudioProvider

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"fake-audio-bytes"
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        provider = StabilityAudioProvider(
            api_key="test-key", output_dir=tempfile.mkdtemp()
        )
        provider._http_client = mock_http
        return provider

    def make_step(self):
        return Step(
            provider="stability-audio",
            model="stable-audio-2.5",
            prompt="test prompt",
        )
