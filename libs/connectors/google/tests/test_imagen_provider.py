"""Tests for ImagenProvider (mocked — no real API calls)."""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


@pytest.fixture
def mock_imagen(tmp_path):
    """Patch google.genai with a mock client."""
    mock_image = MagicMock()
    mock_image.save = MagicMock()

    mock_response = MagicMock()
    mock_response.generated_images = [MagicMock(image=mock_image)]

    mock_client = MagicMock()
    mock_client.models.generate_images.return_value = mock_response

    # Mock the google.genai.types module
    mock_types = MagicMock()
    mock_genai = MagicMock()
    mock_genai.types = mock_types
    mock_google = MagicMock()
    mock_google.genai = mock_genai

    with patch.dict(
        "sys.modules",
        {"google": mock_google, "google.genai": mock_genai, "google.genai.types": mock_types},
    ):
        from genblaze_google.imagen import ImagenProvider

        provider = ImagenProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


def test_generate_returns_image_asset(mock_imagen):
    provider, _ = mock_imagen
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="a mountain landscape",
    )
    result = provider.generate(step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "image/png"
    assert result.assets[0].url.startswith("file://")


def test_invoke_full_lifecycle(mock_imagen):
    provider, _ = mock_imagen
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="a sunset",
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_aspect_ratio_raises(mock_imagen):
    provider, _ = mock_imagen
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="test",
        params={"aspect_ratio": "5:3"},
    )
    with pytest.raises(ProviderError, match="Invalid aspect_ratio"):
        provider.generate(step)


def test_multiple_images(mock_imagen):
    provider, client = mock_imagen
    img1, img2 = MagicMock(), MagicMock()
    img1.image = MagicMock()
    img2.image = MagicMock()
    client.models.generate_images.return_value = MagicMock(generated_images=[img1, img2])
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="cats",
        params={"number_of_images": 2},
    )
    result = provider.generate(step)
    assert len(result.assets) == 2


def test_params_passed_to_api(mock_imagen):
    provider, client = mock_imagen
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="test",
        params={"aspect_ratio": "16:9", "number_of_images": 2},
    )
    provider.generate(step)
    client.models.generate_images.assert_called_once()


def test_empty_images_raises_on_safety_filter(mock_imagen):
    """Imagen safety filter returns 0 images — must raise, not return empty assets."""
    provider, client = mock_imagen
    client.models.generate_images.return_value = MagicMock(generated_images=[])
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="blocked content",
    )
    with pytest.raises(ProviderError, match="no images"):
        provider.generate(step)


def test_api_error_raises_provider_error(mock_imagen):
    provider, client = mock_imagen
    client.models.generate_images.side_effect = RuntimeError("403 permission denied")
    step = Step(
        provider="google-imagen",
        model="imagen-3.0-generate-002",
        prompt="test",
    )
    with pytest.raises(ProviderError, match="Imagen generation failed"):
        provider.generate(step)


# --- Compliance harness ---


class TestImagenCompliance(ProviderComplianceTests):
    """Verify ImagenProvider satisfies the genblaze provider contract."""

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        mock_types = MagicMock()
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
        from genblaze_google.imagen import ImagenProvider

        mock_image = MagicMock()
        mock_image.save = MagicMock()
        mock_response = MagicMock()
        mock_response.generated_images = [MagicMock(image=mock_image)]
        mock_client = MagicMock()
        mock_client.models.generate_images.return_value = mock_response
        provider = ImagenProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(
            provider="google-imagen",
            model="imagen-3.0-generate-002",
            prompt="test prompt",
        )
