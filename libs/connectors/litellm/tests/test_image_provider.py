"""Tests for LiteLLMImageProvider (mocked; no billable requests)."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests
from genblaze_litellm import LiteLLMImageProvider

from .conftest import PNG_BYTES


def _step(**params):
    return Step(
        provider="litellm-image",
        model="vertex_ai/imagen-4.0-generate-001",
        modality=Modality.IMAGE,
        prompt="a lighthouse at dawn",
        params=params,
    )


def test_b64_image_is_written_hashed_and_priced(fake_litellm, tmp_path):
    provider = LiteLLMImageProvider(output_dir=tmp_path)
    result = provider.invoke(_step(size="1024x1024", quality="high"))

    assert result.status == StepStatus.SUCCEEDED
    asset = result.assets[0]
    assert asset.url.startswith("file://")
    assert asset.media_type == "image/png"
    assert asset.sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert asset.size_bytes == len(PNG_BYTES)
    assert result.cost_usd == pytest.approx(0.042)
    assert result.provider_payload["usage"] == {"input_tokens": 12, "output_tokens": 4160}

    call = fake_litellm.kwargs_of("image_generation")[0]
    assert call["model"] == "vertex_ai/imagen-4.0-generate-001"
    assert call["prompt"] == "a lighthouse at dawn"
    assert call["size"] == "1024x1024"
    assert call["quality"] == "high"
    assert call["drop_params"] is True
    # No key configured: LiteLLM must fall back to the provider's env vars.
    assert "api_key" not in call
    assert "api_base" not in call


def test_credentials_forwarded_when_set(fake_litellm):
    provider = LiteLLMImageProvider(
        api_key="sk-test", api_base="https://my-azure.openai.azure.com"
    )
    provider.invoke(_step())
    call = fake_litellm.kwargs_of("image_generation")[0]
    assert call["api_key"] == "sk-test"
    assert call["api_base"] == "https://my-azure.openai.azure.com"


def test_output_format_sets_media_type(fake_litellm, tmp_path):
    result = LiteLLMImageProvider(output_dir=tmp_path).invoke(_step(output_format="webp"))
    assert result.assets[0].media_type == "image/webp"
    assert result.assets[0].url.endswith(".webp")


def test_url_results_are_validated_and_kept(fake_litellm):
    fake_litellm.image_items = [
        SimpleNamespace(b64_json=None, url="https://cdn.example.com/img.png", revised_prompt="r")
    ]
    result = LiteLLMImageProvider().invoke(_step())
    assert result.assets[0].url == "https://cdn.example.com/img.png"
    assert result.assets[0].metadata["revised_prompt"] == "r"


def test_unsafe_result_url_fails_the_step(fake_litellm):
    fake_litellm.image_items = [SimpleNamespace(b64_json=None, url="http://169.254.169.254/x")]
    result = LiteLLMImageProvider().invoke(_step())
    assert result.status == StepStatus.FAILED


def test_empty_response_raises(fake_litellm):
    fake_litellm.image_items = []
    with pytest.raises(ProviderError, match="no images"):
        LiteLLMImageProvider().generate(_step())


class TestLiteLLMImageCompliance(ProviderComplianceTests):
    @pytest.fixture(autouse=True)
    def _fake(self, fake_litellm):
        self.fake = fake_litellm

    def make_provider(self):
        return LiteLLMImageProvider()

    def make_step(self):
        return _step()
