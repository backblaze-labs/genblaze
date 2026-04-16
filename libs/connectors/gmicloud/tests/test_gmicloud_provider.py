"""Tests for GMICloudVideoProvider (mocked — no real API calls)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


def _make_mock_client():
    """Build a mock gmicloud Client with video_manager stubs."""
    mock_client = MagicMock()
    mock_client.video_manager.create_request.return_value = SimpleNamespace(
        request_id="req-abc123",
        status="queued",
    )
    mock_client.video_manager.get_request_detail.return_value = SimpleNamespace(
        request_id="req-abc123",
        status="success",
        outcome={"video_url": "https://gmicloud-output.com/video.mp4"},
    )
    return mock_client


@pytest.fixture
def mock_gmicloud():
    """Patch gmicloud with a mock client."""
    mock_client = _make_mock_client()
    with patch.dict("sys.modules", {"gmicloud": MagicMock()}):
        from genblaze_gmicloud import GMICloudVideoProvider

        provider = GMICloudVideoProvider(email="test@test.com", password="test")
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_request_id(mock_gmicloud):
    provider, client = mock_gmicloud
    step = Step(
        provider="gmicloud",
        model="Kling-Text2Video-V1.6-Pro",
        prompt="a sunset over ocean",
    )
    req_id = provider.submit(step)
    assert req_id == "req-abc123"
    client.video_manager.create_request.assert_called_once()


def test_submit_forwards_params(mock_gmicloud):
    provider, client = mock_gmicloud
    step = Step(
        provider="gmicloud",
        model="Kling-Text2Video-V1.6-Pro",
        prompt="test",
        params={"duration": 10, "cfg_scale": 0.5},
    )
    provider.submit(step)
    call_kwargs = client.video_manager.create_request.call_args[1]
    assert call_kwargs["payload"]["duration"] == 10
    assert call_kwargs["payload"]["cfg_scale"] == 0.5


def test_submit_image_to_video(mock_gmicloud):
    """Image-to-video passes image URL in payload."""
    from genblaze_core.models.asset import Asset

    provider, client = mock_gmicloud
    step = Step(
        provider="gmicloud",
        model="Kling-Image2Video-V1.6-Pro",
        prompt="animate this",
        inputs=[Asset(url="https://example.com/image.png", media_type="image/png")],
    )
    provider.submit(step)
    call_kwargs = client.video_manager.create_request.call_args[1]
    assert call_kwargs["payload"]["image"] == "https://example.com/image.png"


def test_poll_returns_true_on_success(mock_gmicloud):
    provider, _ = mock_gmicloud
    assert provider.poll("req-abc123") is True


def test_poll_returns_false_on_processing(mock_gmicloud):
    provider, client = mock_gmicloud
    client.video_manager.get_request_detail.return_value = SimpleNamespace(
        request_id="req-abc123",
        status="processing",
    )
    assert provider.poll("req-abc123") is False


def test_poll_returns_true_on_failed(mock_gmicloud):
    provider, client = mock_gmicloud
    client.video_manager.get_request_detail.return_value = SimpleNamespace(
        request_id="req-abc123",
        status="failed",
        error="Content policy violation",
    )
    assert provider.poll("req-abc123") is True


def test_fetch_output_attaches_asset(mock_gmicloud):
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    assert "gmicloud-output.com" in result.assets[0].url


def test_fetch_output_failed_raises(mock_gmicloud):
    provider, client = mock_gmicloud
    client.video_manager.get_request_detail.return_value = SimpleNamespace(
        request_id="req-abc123",
        status="failed",
        error="Content policy violation",
    )
    # Clear the poll cache so fetch_output re-fetches
    provider._poll_cache = {}
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="bad")
    with pytest.raises(ProviderError, match="Content policy violation"):
        provider.fetch_output("req-abc123", step)


def test_fetch_output_cancelled_raises(mock_gmicloud):
    provider, client = mock_gmicloud
    client.video_manager.get_request_detail.return_value = SimpleNamespace(
        request_id="req-abc123",
        status="cancelled",
    )
    provider._poll_cache = {}
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="test")
    with pytest.raises(ProviderError, match="cancelled"):
        provider.fetch_output("req-abc123", step)


def test_invoke_full_lifecycle(mock_gmicloud):
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_cost_tracked(mock_gmicloud):
    """Cost is set based on model."""
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd is not None
    assert result.cost_usd == 0.30


def test_cost_tracked_v15(mock_gmicloud):
    """V1.5 model has lower cost."""
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.5-Pro", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd == 0.20


def test_cost_none_unknown_model(mock_gmicloud):
    """Cost stays None for unknown model."""
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="unknown-model", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd is None


def test_video_metadata_populated(mock_gmicloud):
    """Video metadata is set on output assets."""
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    assert result.assets[0].video is not None
    assert result.assets[0].video.has_audio is False


def test_provider_payload_populated(mock_gmicloud):
    """Provider payload includes request metadata."""
    provider, _ = mock_gmicloud
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    assert "gmicloud" in result.provider_payload
    assert result.provider_payload["gmicloud"]["request_id"] == "req-abc123"
    assert result.provider_payload["gmicloud"]["status"] == "success"


def test_error_mapping_auth():
    """Auth errors map to AUTH_FAILURE."""
    from genblaze_core.models.enums import ProviderErrorCode
    from genblaze_gmicloud._errors import map_gmicloud_error

    exc = Exception("unauthorized: invalid credentials")
    assert map_gmicloud_error(exc) == ProviderErrorCode.AUTH_FAILURE


def test_normalize_params_duration(mock_gmicloud):
    """Duration is cast to int."""
    provider, _ = mock_gmicloud
    result = provider.normalize_params({"duration": "10"})
    assert result["duration"] == 10


def test_normalize_params_guidance_scale(mock_gmicloud):
    """guidance_scale maps to cfg_scale."""
    provider, _ = mock_gmicloud
    result = provider.normalize_params({"guidance_scale": 0.7})
    assert result["cfg_scale"] == 0.7
    assert "guidance_scale" not in result


def test_normalize_params_idempotent(mock_gmicloud):
    """normalize_params(normalize_params(p)) == normalize_params(p)."""
    provider, _ = mock_gmicloud
    params = {"duration": 10, "guidance_scale": 0.7}
    once = provider.normalize_params(params)
    twice = provider.normalize_params(once)
    assert once == twice


# --- Compliance harness ---


class TestGMICloudCompliance(ProviderComplianceTests):
    """Verify GMICloudVideoProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"gmicloud": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_gmicloud import GMICloudVideoProvider

        mock_client = _make_mock_client()
        provider = GMICloudVideoProvider(email="test@test.com", password="test")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="test prompt")
