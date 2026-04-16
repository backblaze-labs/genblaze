"""Tests for VeoProvider (mocked — no real API calls)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


def _make_completed_operation():
    """Create a mock completed operation with a generated video."""
    video = SimpleNamespace(uri="https://storage.googleapis.com/video/out.mp4")
    gv = SimpleNamespace(video=video)
    response = SimpleNamespace(generated_videos=[gv])
    return SimpleNamespace(done=True, name="op-123", error=None, response=response)


def _make_pending_operation():
    return SimpleNamespace(done=False, name="op-123", error=None, response=None)


@pytest.fixture
def mock_google():
    """Patch google.genai with a mock client."""
    mock_types = MagicMock()
    mock_types.GenerateVideosConfig = MagicMock

    mock_genai = MagicMock()
    mock_google_mod = MagicMock()
    mock_google_mod.genai = mock_genai

    mock_client = MagicMock()
    mock_client.models.generate_videos.return_value = _make_pending_operation()
    mock_client.operations.get.return_value = _make_completed_operation()
    mock_client.files.download.return_value = None

    with patch.dict(
        "sys.modules",
        {
            "google": mock_google_mod,
            "google.genai": mock_genai,
            "google.genai.types": mock_types,
        },
    ):
        from genblaze_google import VeoProvider

        provider = VeoProvider(api_key="test-key")
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_operation_name(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    # Returns the provider-native operation name, not step_id
    assert pred_id == "op-123"
    client.models.generate_videos.assert_called_once()


def test_poll_returns_false_when_pending(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_pending_operation()
    assert provider.poll(pred_id) is False


def test_poll_returns_true_when_done(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    assert provider.poll(pred_id) is True


def test_fetch_output_attaches_asset(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    # Poll to cache the completed operation
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    assert "storage.googleapis.com" in result.assets[0].url


def test_fetch_output_error_raises(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="bad")
    pred_id = provider.submit(step)
    error_op = SimpleNamespace(
        done=True, name="op-err", error="Safety filter triggered", response=None
    )
    client.operations.get.return_value = error_op
    provider.poll(pred_id)

    with pytest.raises(ProviderError, match="Safety filter"):
        provider.fetch_output(pred_id, step)


def test_invoke_full_lifecycle(mock_google):
    """Full invoke() succeeds with mocked client."""
    provider, client = mock_google
    client.operations.get.return_value = _make_completed_operation()

    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_aspect_ratio_raises(mock_google):
    provider, _ = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="test",
        params={"aspect_ratio": "4:3"},
    )
    with pytest.raises(ProviderError, match="Invalid aspect_ratio"):
        provider.submit(step)


def test_invalid_resolution_raises(mock_google):
    provider, _ = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="test",
        params={"resolution": "360p"},
    )
    with pytest.raises(ProviderError, match="Invalid resolution"):
        provider.submit(step)


def test_resume_works_with_operation_name(mock_google):
    """resume() works with just the operation name (no in-memory state needed)."""
    provider, client = mock_google
    client.operations.get.return_value = _make_completed_operation()
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    # resume with just the operation name — no prior submit() needed
    result = provider.resume("op-123", step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_duration_alias(mock_google):
    """Standard 'duration' param is aliased to 'duration_seconds' for Veo."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="test",
        params={"duration": "6"},
    )
    provider.submit(step)
    client.models.generate_videos.assert_called_once()


def test_cost_tracked(mock_google):
    """Cost is set based on model and duration."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
        params={"duration_seconds": "6"},
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.cost_usd == pytest.approx(0.35 * 6)


def test_cost_tracked_duration_alias(mock_google):
    """Cost uses the standard 'duration' alias."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-3.0-generate-001",
        prompt="a sunset",
        params={"duration": "8"},
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.cost_usd == pytest.approx(0.50 * 8)


def test_cost_default_duration(mock_google):
    """Cost uses default 4s when no duration specified."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.cost_usd == pytest.approx(0.35 * 4)


def test_cost_none_unknown_model(mock_google):
    """Cost stays None for unknown model."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="unknown-veo-model",
        prompt="a sunset",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.cost_usd is None


def test_veo3_populates_tracks(mock_google):
    """Veo 3 models populate multi-track metadata on assets."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-3.0-generate-001",
        prompt="a sunset with music",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.tracks is not None
    assert len(asset.tracks) == 2
    assert asset.tracks[0].kind == "video"
    assert asset.tracks[0].codec == "h264"
    assert asset.tracks[1].kind == "audio"
    assert asset.tracks[1].codec == "aac"
    assert asset.tracks[1].label == "generated-audio"


def test_veo3_fast_populates_tracks(mock_google):
    """Veo 3 fast model also populates tracks."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-3.0-fast-generate-001",
        prompt="quick clip",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.assets[0].tracks is not None
    assert len(result.assets[0].tracks) == 2


def test_veo2_no_tracks(mock_google):
    """Veo 2 models do not populate tracks."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.assets[0].tracks is None


# --- Compliance harness ---


class TestVeoCompliance(ProviderComplianceTests):
    """Verify VeoProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        mock_types = MagicMock()
        mock_types.GenerateVideosConfig = MagicMock
        mock_genai = MagicMock()
        mock_google_mod = MagicMock()
        mock_google_mod.genai = mock_genai
        with patch.dict(
            "sys.modules",
            {
                "google": mock_google_mod,
                "google.genai": mock_genai,
                "google.genai.types": mock_types,
            },
        ):
            yield

    def make_provider(self):
        from genblaze_google import VeoProvider

        mock_client = MagicMock()
        mock_client.models.generate_videos.return_value = _make_pending_operation()
        mock_client.operations.get.return_value = _make_completed_operation()
        mock_client.files.download.return_value = None
        provider = VeoProvider(api_key="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(
            provider="google-veo",
            model="veo-2.0-generate-001",
            prompt="test prompt",
        )
