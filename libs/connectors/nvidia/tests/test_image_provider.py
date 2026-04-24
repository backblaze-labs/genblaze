"""Tests for NvidiaImageProvider."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests

from .conftest import make_mock_http_client

_PNG_B64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000001e221bc330000000049454e44ae426082"
    )
).decode()


@pytest.fixture
def provider(tmp_path):
    from genblaze_nvidia import NvidiaImageProvider

    p = NvidiaImageProvider(api_key="nvapi-test", output_dir=tmp_path)
    p._client._http_client = make_mock_http_client(
        submit_body={"artifacts": [{"base64": _PNG_B64, "mime_type": "image/png"}]},
    )
    return p


# --- Generate ---


def test_generate_writes_image_file(provider, tmp_path):
    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="a cat",
    )
    result = provider.generate(step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "image/png"
    assert asset.url.startswith("file://")
    # File landed in our tmp_path output dir.
    path = Path(asset.url.removeprefix("file://"))
    assert path.parent == tmp_path


def test_generate_forwards_prompt_and_params(provider):
    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="a cat",
        params={"cfg_scale": 4.5, "aspect_ratio": "16:9"},
    )
    provider.generate(step)
    body = provider._client._http_client.post.call_args.kwargs.get("json")
    assert body["prompt"] == "a cat"
    assert body["cfg_scale"] == 4.5
    assert body["aspect_ratio"] == "16:9"


def test_sdxl_transformer_wraps_text_prompts(provider):
    """SDXL's spec rewrites ``prompt`` into ``text_prompts`` before the wire send."""
    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-xl",
        prompt="a cat",
        negative_prompt="blurry",
    )
    provider.generate(step)
    body = provider._client._http_client.post.call_args.kwargs.get("json")
    assert "prompt" not in body
    assert body["text_prompts"] == [
        {"text": "a cat", "weight": 1.0},
        {"text": "blurry", "weight": -1.0},
    ]


def test_guidance_scale_alias(provider):
    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="t",
        params={"guidance_scale": 3.0},
    )
    provider.generate(step)
    body = provider._client._http_client.post.call_args.kwargs.get("json")
    assert body["cfg_scale"] == 3.0
    assert "guidance_scale" not in body


def test_generate_hosted_url_preferred(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"artifacts": [{"url": "https://nvcf.example/img.png"}]}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="t",
    )
    result = provider.generate(step)
    assert result.assets[0].url == "https://nvcf.example/img.png"


def test_generate_rejects_http_hosted_url(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"artifacts": [{"url": "http://insecure.example/img.png"}]}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="t",
    )
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.generate(step)


def test_generate_no_output_raises(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="t",
    )
    with pytest.raises(ProviderError, match="no asset URL or base64"):
        provider.generate(step)


def test_generate_handles_400_policy_as_content_policy(provider):
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {"detail": "blocked by safety filter"}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="unsafe",
    )
    from genblaze_core.models.enums import ProviderErrorCode

    with pytest.raises(ProviderError) as info:
        provider.generate(step)
    assert info.value.error_code == ProviderErrorCode.CONTENT_POLICY


def test_invoke_full_lifecycle(provider):
    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="a cat",
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_multi_artifact_output(provider):
    """N artifacts in response → N assets."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "artifacts": [
            {"base64": _PNG_B64, "mime_type": "image/png"},
            {"base64": _PNG_B64, "mime_type": "image/png"},
        ]
    }
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(
        provider="nvidia-image",
        model="stabilityai/stable-diffusion-3-5-large",
        prompt="t",
    )
    result = provider.generate(step)
    assert len(result.assets) == 2


# --- Pass-through for unknown models ---


def test_unknown_model_passthrough(provider):
    step = Step(
        provider="nvidia-image",
        model="some-vendor/unreleased-image-v99",
        prompt="test",
    )
    result = provider.generate(step)
    assert len(result.assets) == 1


# --- Compliance ---


class TestNvidiaImageCompliance(ProviderComplianceTests):
    # NIM free tier is RPM-gated with no per-image billing. Pricing opt-out.
    expects_cost = False

    def make_provider(self):
        import tempfile

        from genblaze_nvidia import NvidiaImageProvider

        p = NvidiaImageProvider(api_key="nvapi-compliance", output_dir=Path(tempfile.mkdtemp()))
        p._client._http_client = make_mock_http_client(
            submit_body={"artifacts": [{"base64": _PNG_B64, "mime_type": "image/png"}]},
        )
        return p

    def make_step(self):
        return Step(
            provider="nvidia-image",
            model="stabilityai/stable-diffusion-3-5-large",
            prompt="test",
        )
