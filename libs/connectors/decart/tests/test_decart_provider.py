"""Tests for DecartVideoProvider and DecartImageProvider (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests

# -- Fixtures --


@pytest.fixture
def mock_decart_mod():
    """Shared mock for the decart module."""
    mock_mod = MagicMock()
    mock_models = MagicMock()
    mock_models.video.return_value = "lucy-pro-t2v"
    mock_models.image.return_value = "lucy-pro-t2i"
    mock_mod.models = mock_models
    return mock_mod


@pytest.fixture
def mock_video_provider(tmp_path, mock_decart_mod):
    """Patch decart with a mock client for video generation."""
    mock_client = MagicMock()
    mock_client.queue.submit = AsyncMock(return_value=SimpleNamespace(job_id="job-abc"))
    mock_client.queue.status = AsyncMock(return_value=SimpleNamespace(status="completed"))
    mock_client.queue.result = AsyncMock(return_value=SimpleNamespace(data=b"fake-video-data"))

    with patch.dict("sys.modules", {"decart": mock_decart_mod}):
        from genblaze_decart import DecartVideoProvider

        provider = DecartVideoProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


@pytest.fixture
def mock_image_provider(tmp_path, mock_decart_mod):
    """Patch decart with a mock client for image generation."""
    mock_client = MagicMock()
    mock_client.process = AsyncMock(return_value=SimpleNamespace(data=b"fake-image-data"))

    with patch.dict("sys.modules", {"decart": mock_decart_mod}):
        from genblaze_decart import DecartImageProvider

        provider = DecartImageProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


# -- Video provider tests --


def test_submit_video_returns_job_id(mock_video_provider):
    provider, client = mock_video_provider
    step = Step(provider="decart", model="lucy-pro-t2v", prompt="a sunset")
    job_id = provider.submit(step)
    assert job_id == "job-abc"
    client.queue.submit.assert_awaited_once()


def test_poll_returns_true_on_completed(mock_video_provider):
    provider, _ = mock_video_provider
    assert provider.poll("job-abc") is True


def test_poll_returns_false_on_processing(mock_video_provider):
    provider, client = mock_video_provider
    client.queue.status = AsyncMock(return_value=SimpleNamespace(status="processing"))
    assert provider.poll("job-abc") is False


def test_fetch_video_output(mock_video_provider):
    provider, _ = mock_video_provider
    step = Step(provider="decart", model="lucy-pro-t2v", prompt="a sunset")
    result = provider.fetch_output("job-abc", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    assert result.assets[0].url.startswith("file://")


def test_fetch_output_failed_raises(mock_video_provider):
    provider, client = mock_video_provider
    client.queue.status = AsyncMock(
        return_value=SimpleNamespace(status="failed", error="Content moderation")
    )
    step = Step(provider="decart", model="lucy-pro-t2v", prompt="bad")
    with pytest.raises(ProviderError, match="Content moderation"):
        provider.fetch_output("job-abc", step)


def test_video_cost_tracked(mock_video_provider):
    """Video cost is set based on resolution."""
    provider, _ = mock_video_provider
    step = Step(
        provider="decart",
        model="lucy-pro-t2v",
        prompt="a sunset",
        params={"resolution": "720p"},
    )
    result = provider.fetch_output("job-abc", step)
    assert result.cost_usd is not None
    assert result.cost_usd == 0.08


def test_video_capabilities(mock_video_provider):
    """Video provider declares VIDEO modality only."""
    provider, _ = mock_video_provider
    from genblaze_core.models.enums import Modality

    caps = provider.get_capabilities()
    assert caps is not None
    assert caps.supported_modalities == [Modality.VIDEO]
    assert caps.accepts_chain_input is True


# -- Image provider tests --


def test_image_generate_returns_asset(mock_image_provider):
    provider, _ = mock_image_provider
    step = Step(provider="decart-image", model="lucy-pro-t2i", prompt="a cat")
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "image/png"
    assert result.assets[0].url.startswith("file://")


def test_image_cost_tracked(mock_image_provider):
    """Image cost is flat rate."""
    provider, _ = mock_image_provider
    step = Step(provider="decart-image", model="lucy-pro-t2i", prompt="a cat")
    result = provider.generate(step)
    assert result.cost_usd == 0.02


def test_image_capabilities(mock_image_provider):
    """Image provider declares IMAGE modality only."""
    provider, _ = mock_image_provider
    from genblaze_core.models.enums import Modality

    caps = provider.get_capabilities()
    assert caps is not None
    assert caps.supported_modalities == [Modality.IMAGE]
    assert caps.accepts_chain_input is False


# -- Backward compatibility --


def test_backward_compat_import(mock_decart_mod):
    """DecartProvider alias still works for backward compat."""
    with patch.dict("sys.modules", {"decart": mock_decart_mod}):
        from genblaze_decart import DecartProvider, DecartVideoProvider

        assert DecartProvider is DecartVideoProvider


# --- Compliance harness ---


class TestDecartVideoCompliance(ProviderComplianceTests):
    """Verify DecartVideoProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self, mock_decart_mod):
        with patch.dict("sys.modules", {"decart": mock_decart_mod}):
            yield

    def make_provider(self):
        from genblaze_decart import DecartVideoProvider

        mock_client = MagicMock()
        mock_client.queue.submit = AsyncMock(
            return_value=SimpleNamespace(job_id="job-abc")
        )
        mock_client.queue.status = AsyncMock(
            return_value=SimpleNamespace(status="completed")
        )
        mock_client.queue.result = AsyncMock(
            return_value=SimpleNamespace(data=b"fake-video-data")
        )
        provider = DecartVideoProvider(
            api_key="test-key", output_dir=tempfile.mkdtemp()
        )
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="decart", model="lucy-pro-t2v", prompt="test prompt")


class TestDecartImageCompliance(ProviderComplianceTests):
    """Verify DecartImageProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self, mock_decart_mod):
        with patch.dict("sys.modules", {"decart": mock_decart_mod}):
            yield

    def make_provider(self):
        from genblaze_decart import DecartImageProvider

        mock_client = MagicMock()
        mock_client.process = AsyncMock(
            return_value=SimpleNamespace(data=b"fake-image-data")
        )
        provider = DecartImageProvider(
            api_key="test-key", output_dir=tempfile.mkdtemp()
        )
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="decart-image", model="lucy-pro-t2i", prompt="test prompt")
