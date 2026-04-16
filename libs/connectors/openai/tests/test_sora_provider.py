"""Tests for SoraProvider (mocked — no real API calls)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_openai():
    """Patch openai module with a mock client."""
    mock_client = MagicMock()

    # Mock videos.create → returns job with id
    mock_client.videos.create.return_value = SimpleNamespace(id="vid-abc123")

    # Mock videos.retrieve → returns completed video
    mock_client.videos.retrieve.return_value = SimpleNamespace(
        id="vid-abc123",
        status="completed",
        model="sora-2",
    )

    # Mock videos.content → returns writable response
    mock_content = MagicMock()

    def _write_video(path):
        with open(path, "wb") as f:
            f.write(b"video")

    mock_content.write_to_file = MagicMock(side_effect=_write_video)
    mock_client.videos.content.return_value = mock_content

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test-key")
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_video_id(mock_openai):
    provider, client = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    vid_id = provider.submit(step)
    assert vid_id == "vid-abc123"
    client.videos.create.assert_called_once()


def test_poll_returns_true_on_completed(mock_openai):
    provider, _ = mock_openai
    assert provider.poll("vid-abc123") is True


def test_poll_returns_false_on_in_progress(mock_openai):
    provider, client = mock_openai
    client.videos.retrieve.return_value = SimpleNamespace(id="vid-abc123", status="in_progress")
    assert provider.poll("vid-abc123") is False


def test_fetch_output_attaches_asset(mock_openai):
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    result = provider.fetch_output("vid-abc123", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    # Now saves locally as file:// URI instead of unauthenticated API URL
    assert result.assets[0].url.startswith("file://")


def test_fetch_output_failed_raises(mock_openai):
    provider, client = mock_openai
    client.videos.retrieve.return_value = SimpleNamespace(
        id="vid-abc123", status="failed", error="Content policy violation"
    )
    step = Step(provider="openai-sora", model="sora-2", prompt="bad prompt")
    with pytest.raises(ProviderError, match="Content policy violation"):
        provider.fetch_output("vid-abc123", step)


def test_invoke_full_lifecycle(mock_openai):
    """Full invoke() succeeds with mocked client."""
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_seconds_raises(mock_openai):
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="test", params={"seconds": 7})
    with pytest.raises(ProviderError, match="Invalid seconds"):
        provider.submit(step)


def test_invalid_size_raises(mock_openai):
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="test", params={"size": "500x500"})
    with pytest.raises(ProviderError, match="Invalid size"):
        provider.submit(step)


def test_submit_with_image_input(mock_openai):
    """Image-to-video: image URL is forwarded to the API as 'image' param."""
    from genblaze_core.models.asset import Asset

    provider, client = mock_openai
    img = Asset(url="https://example.com/frame.png", media_type="image/png")
    step = Step(provider="openai-sora", model="sora-2", prompt="animate this", inputs=[img])
    vid_id = provider.submit(step)
    assert vid_id == "vid-abc123"
    call_kwargs = client.videos.create.call_args[1]
    assert call_kwargs["image"] == "https://example.com/frame.png"


def test_submit_without_inputs_still_works(mock_openai):
    """Text-only generation works when no inputs are provided."""
    provider, client = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset over the ocean")
    vid_id = provider.submit(step)
    assert vid_id == "vid-abc123"
    call_kwargs = client.videos.create.call_args[1]
    assert "image" not in call_kwargs


def test_submit_skips_non_image_inputs(mock_openai):
    """Non-image inputs (e.g. video) are ignored; no 'image' param is sent."""
    from genblaze_core.models.asset import Asset

    provider, client = mock_openai
    vid_asset = Asset(url="https://example.com/clip.mp4", media_type="video/mp4")
    step = Step(provider="openai-sora", model="sora-2", prompt="extend this", inputs=[vid_asset])
    provider.submit(step)
    call_kwargs = client.videos.create.call_args[1]
    assert "image" not in call_kwargs


def test_submit_rejects_unsafe_chain_input_url(mock_openai):
    """Chain input URLs must be HTTPS or file:// — http:// is rejected."""
    from genblaze_core.models.asset import Asset

    provider, _ = mock_openai
    img = Asset(url="http://evil.com/payload.png", media_type="image/png")
    step = Step(provider="openai-sora", model="sora-2", prompt="animate", inputs=[img])
    with pytest.raises(ProviderError, match="Unsafe chain input URL"):
        provider.submit(step)


# --- Compliance harness ---


class TestSoraCompliance(ProviderComplianceTests):
    """Verify SoraProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"openai": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_openai import SoraProvider

        mock_content = MagicMock()
        mock_content.write_to_file = MagicMock(
            side_effect=lambda path: open(path, "wb").write(b"video")
        )
        mock_client = MagicMock()
        mock_client.videos.create.return_value = SimpleNamespace(id="vid-abc123")
        mock_client.videos.retrieve.return_value = SimpleNamespace(
            id="vid-abc123", status="completed", model="sora-2"
        )
        mock_client.videos.content.return_value = mock_content
        provider = SoraProvider(api_key="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="openai-sora", model="sora-2", prompt="test prompt")
