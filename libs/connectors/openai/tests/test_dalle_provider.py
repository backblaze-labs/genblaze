"""Tests for DalleProvider (mocked — no real API calls)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_dalle():
    """Patch openai with a mock client."""
    mock_client = MagicMock()
    mock_client.images.generate.return_value = SimpleNamespace(
        data=[SimpleNamespace(url="https://oaidalleapiprodscus.blob.core.windows.net/img.png")]
    )

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai.dalle import DalleProvider

        provider = DalleProvider(api_key="test-key")
        provider._client = mock_client
        yield provider, mock_client


def test_generate_returns_image_asset(mock_dalle):
    provider, _ = mock_dalle
    step = Step(provider="openai-dalle", model="dall-e-3", prompt="a cat in space")
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "image/png"
    assert "blob.core.windows.net" in result.assets[0].url


def test_invoke_full_lifecycle(mock_dalle):
    provider, _ = mock_dalle
    step = Step(provider="openai-dalle", model="dall-e-3", prompt="a cat")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_multiple_images(mock_dalle):
    provider, client = mock_dalle
    client.images.generate.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(url="https://example.com/img1.png"),
            SimpleNamespace(url="https://example.com/img2.png"),
        ]
    )
    step = Step(provider="openai-dalle", model="dall-e-3", prompt="cats", params={"n": 2})
    result = provider.generate(step)
    assert len(result.assets) == 2


def test_params_passed_to_api(mock_dalle):
    provider, client = mock_dalle
    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="test",
        params={"size": "1792x1024", "quality": "hd", "style": "vivid"},
    )
    provider.generate(step)
    call_kwargs = client.images.generate.call_args[1]
    assert call_kwargs["size"] == "1792x1024"
    assert call_kwargs["quality"] == "hd"
    assert call_kwargs["style"] == "vivid"


def test_api_error_raises_provider_error(mock_dalle):
    provider, client = mock_dalle
    client.images.generate.side_effect = RuntimeError("429 rate limit exceeded")
    step = Step(provider="openai-dalle", model="dall-e-3", prompt="test")
    with pytest.raises(ProviderError, match="DALL-E generation failed"):
        provider.generate(step)


# --- Cost tracking ---


def test_cost_tracked_dalle3_standard(mock_dalle):
    """Cost computed for DALL-E 3 standard quality at 1024x1024."""
    provider, _ = mock_dalle
    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="test",
        params={"quality": "standard", "size": "1024x1024"},
    )
    result = provider.generate(step)
    assert result.cost_usd == pytest.approx(0.040)


def test_cost_tracked_dalle3_hd(mock_dalle):
    """Cost computed for DALL-E 3 HD quality at 1792x1024."""
    provider, _ = mock_dalle
    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="test",
        params={"quality": "hd", "size": "1792x1024"},
    )
    result = provider.generate(step)
    assert result.cost_usd == pytest.approx(0.120)


def test_cost_none_unknown_model(mock_dalle):
    """Cost stays None for unknown model."""
    provider, _ = mock_dalle
    step = Step(provider="openai-dalle", model="unknown-model", prompt="test")
    result = provider.generate(step)
    assert result.cost_usd is None


# --- Param validation ---


def test_invalid_size_raises(mock_dalle):
    """Invalid size for DALL-E 3 is rejected before API call."""
    provider, _ = mock_dalle
    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="test",
        params={"size": "500x500"},
    )
    with pytest.raises(ProviderError, match="Invalid size"):
        provider.generate(step)


def test_invalid_quality_raises(mock_dalle):
    """Invalid quality value is rejected before API call."""
    provider, _ = mock_dalle
    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="test",
        params={"quality": "ultra"},
    )
    with pytest.raises(ProviderError, match="Invalid quality"):
        provider.generate(step)


# --- Capabilities ---


def test_capabilities_declared(mock_dalle):
    """DALL-E provider declares IMAGE modality and model list."""
    provider, _ = mock_dalle
    from genblaze_core.models.enums import Modality

    caps = provider.get_capabilities()
    assert caps is not None
    assert caps.supported_modalities == [Modality.IMAGE]
    assert "gpt-image-1" in caps.models
    assert "dall-e-3" in caps.models


# --- gpt-image-1 b64 path ---


def test_gpt_image_1_saves_b64_locally(tmp_path):
    """gpt-image-1 returns base64 data that gets saved as a local file."""
    b64_data = base64.b64encode(b"fake-png-data").decode()
    mock_client = MagicMock()
    mock_client.images.generate.return_value = SimpleNamespace(
        data=[SimpleNamespace(b64_json=b64_data, url=None)]
    )

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai.dalle import DalleProvider

        provider = DalleProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client

    step = Step(provider="openai-dalle", model="gpt-image-1", prompt="a cat")
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].url.startswith("file://")
    assert result.assets[0].media_type == "image/png"


# --- Compliance harness ---


class TestDalleCompliance(ProviderComplianceTests):
    """Verify DalleProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"openai": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_openai.dalle import DalleProvider

        mock_client = MagicMock()
        mock_client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(url="https://example.com/img.png")]
        )
        provider = DalleProvider(api_key="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="openai-dalle", model="dall-e-3", prompt="test prompt")
