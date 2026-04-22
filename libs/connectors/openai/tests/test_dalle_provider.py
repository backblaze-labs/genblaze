"""Tests for DalleProvider (mocked — no real API calls)."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
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
    with pytest.raises(ProviderError, match="OpenAI image generation failed"):
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


# --- New model registry: gpt-image-2 / 1.5 / 1-mini ---


@pytest.fixture
def mock_b64_dalle(tmp_path):
    """DalleProvider with a b64-returning mock client and temp output_dir."""
    b64 = base64.b64encode(b"fake-image-bytes").decode()
    mock_client = MagicMock()
    mock_client.images.generate.return_value = SimpleNamespace(
        data=[SimpleNamespace(b64_json=b64, url=None)]
    )
    mock_client.images.edit.return_value = SimpleNamespace(
        data=[SimpleNamespace(b64_json=b64, url=None)]
    )
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai.dalle import DalleProvider

        provider = DalleProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client, tmp_path


def test_capabilities_lists_all_models(mock_dalle):
    provider, _ = mock_dalle
    caps = provider.get_capabilities()
    for m in (
        "gpt-image-2",
        "gpt-image-1.5",
        "gpt-image-1",
        "gpt-image-1-mini",
        "dall-e-3",
        "dall-e-2",
    ):
        assert m in caps.models
    assert caps.accepts_chain_input is True
    assert "image" in (caps.supported_inputs or [])


def test_capabilities_output_formats_include_webp(mock_dalle):
    provider, _ = mock_dalle
    caps = provider.get_capabilities()
    assert set(caps.output_formats or []) >= {"image/png", "image/jpeg", "image/webp"}


# --- gpt-image-2 free-form size constraints ---


@pytest.mark.parametrize(
    "size",
    ["1024x1024", "1536x1024", "1024x1536", "2048x2048", "2048x1152", "auto"],
)
def test_gpt_image_2_valid_sizes_accepted(mock_b64_dalle, size):
    provider, _, _ = mock_b64_dalle
    step = Step(provider="openai-dalle", model="gpt-image-2", prompt="x", params={"size": size})
    result = provider.generate(step)
    assert result.assets  # no validation error


@pytest.mark.parametrize(
    ("size", "reason"),
    [
        ("3840x2160", "max edge"),  # max edge must be < 3840
        ("1025x1024", "multiples of 16"),  # not multiple of 16
        ("3456x1024", "aspect ratio"),  # 3.375:1 — over 3:1
        ("512x512", "total pixels"),  # 262,144 pixels — under 655,360
        ("notasize", "WIDTHxHEIGHT"),  # malformed
    ],
)
def test_gpt_image_2_invalid_sizes_rejected(mock_b64_dalle, size, reason):
    provider, _, _ = mock_b64_dalle
    step = Step(provider="openai-dalle", model="gpt-image-2", prompt="x", params={"size": size})
    with pytest.raises(ProviderError, match=reason):
        provider.generate(step)


# --- input_fidelity soft-warn ---


def test_input_fidelity_soft_warns_on_unsupported_model(mock_b64_dalle, caplog):
    provider, _, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="gpt-image-1-mini",
        prompt="x",
        params={"input_fidelity": "high"},
    )
    with caplog.at_level("WARNING", logger="genblaze.openai.dalle"):
        provider.generate(step)
    assert any("input_fidelity" in r.message for r in caplog.records)


def test_input_fidelity_no_warn_on_supported_model(mock_b64_dalle, caplog):
    provider, _, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="gpt-image-1.5",
        prompt="x",
        params={"input_fidelity": "high"},
    )
    with caplog.at_level("WARNING", logger="genblaze.openai.dalle"):
        provider.generate(step)
    assert not any("input_fidelity" in r.message for r in caplog.records)


# --- output_format → suffix + media_type ---


def test_output_format_webp_sets_media_type_and_suffix(mock_b64_dalle):
    provider, _, tmp_path = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="gpt-image-1",
        prompt="x",
        params={"output_format": "webp"},
    )
    result = provider.generate(step)
    assert result.assets[0].media_type == "image/webp"
    assert result.assets[0].url.endswith(".webp")


def test_output_format_jpeg_sets_jpg_suffix(mock_b64_dalle):
    provider, _, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="gpt-image-1",
        prompt="x",
        params={"output_format": "jpeg"},
    )
    result = provider.generate(step)
    assert result.assets[0].media_type == "image/jpeg"
    assert result.assets[0].url.endswith(".jpg")


def test_output_compression_out_of_range_rejected(mock_b64_dalle):
    provider, _, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="gpt-image-1",
        prompt="x",
        params={"output_format": "jpeg", "output_compression": 150},
    )
    with pytest.raises(ProviderError, match="output_compression"):
        provider.generate(step)


# --- Edit endpoint routing ---


def test_edit_route_used_when_inputs_present(mock_b64_dalle, tmp_path):
    """step.inputs presence triggers client.images.edit, not generate."""
    provider, client, out_dir = mock_b64_dalle
    input_file = out_dir / "input.png"
    input_file.write_bytes(b"fake-input-png")

    from genblaze_core.models.asset import Asset as _Asset

    step = Step(
        provider="openai-dalle",
        model="gpt-image-2",
        prompt="make it blue",
        inputs=[_Asset(url=f"file://{input_file}", media_type="image/png")],
    )
    result = provider.generate(step)
    assert client.images.edit.called
    assert not client.images.generate.called
    assert len(result.assets) == 1


def test_edit_route_forwards_multiple_inputs(mock_b64_dalle, tmp_path):
    provider, client, out_dir = mock_b64_dalle
    f1 = out_dir / "a.png"
    f1.write_bytes(b"a")
    f2 = out_dir / "b.png"
    f2.write_bytes(b"b")
    from genblaze_core.models.asset import Asset as _Asset

    step = Step(
        provider="openai-dalle",
        model="gpt-image-2",
        prompt="composite",
        inputs=[
            _Asset(url=f"file://{f1}", media_type="image/png"),
            _Asset(url=f"file://{f2}", media_type="image/png"),
        ],
    )
    provider.generate(step)
    kwargs = client.images.edit.call_args.kwargs
    assert isinstance(kwargs["image"], list)
    assert len(kwargs["image"]) == 2


def test_edit_with_dalle3_no_client_gate(mock_dalle, tmp_path):
    """dall-e-3 + inputs: no client-side rejection; server is the authority."""
    provider, client = mock_dalle
    # Put an input on a dall-e-3 step; we only assert the client call was made.
    out_dir = Path(tempfile.mkdtemp())
    f = out_dir / "x.png"
    f.write_bytes(b"p")
    provider._output_dir = out_dir
    from genblaze_core.models.asset import Asset as _Asset

    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="x",
        inputs=[_Asset(url=f"file://{f}", media_type="image/png")],
    )
    provider.generate(step)
    assert client.images.edit.called


def test_edit_mask_param_opens_file(mock_b64_dalle, tmp_path):
    provider, client, out_dir = mock_b64_dalle
    img = out_dir / "i.png"
    img.write_bytes(b"img")
    msk = out_dir / "m.png"
    msk.write_bytes(b"msk")
    from genblaze_core.models.asset import Asset as _Asset

    step = Step(
        provider="openai-dalle",
        model="gpt-image-2",
        prompt="x",
        inputs=[_Asset(url=f"file://{img}", media_type="image/png")],
        params={"mask": f"file://{msk}"},
    )
    provider.generate(step)
    kwargs = client.images.edit.call_args.kwargs
    assert "mask" in kwargs
    assert kwargs["mask"] is not None  # file handle was passed


def test_edit_file_url_outside_allowed_roots_rejected(mock_b64_dalle):
    provider, _, _ = mock_b64_dalle
    from genblaze_core.models.asset import Asset as _Asset

    step = Step(
        provider="openai-dalle",
        model="gpt-image-2",
        prompt="x",
        inputs=[_Asset(url="file:///etc/passwd", media_type="image/png")],
    )
    with pytest.raises(ProviderError, match="outside allowed"):
        provider.generate(step)


# --- Unknown model passthrough ---


def test_unknown_model_passes_through(mock_b64_dalle):
    """Unknown / alias models route without validation; cost stays None."""
    provider, client, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="chatgpt-image-latest",
        prompt="x",
        params={"size": "4096x4096"},  # would fail gpt-image-2 validator
    )
    result = provider.generate(step)
    assert client.images.generate.called
    assert result.cost_usd is None


# --- New-model pricing ---


@pytest.mark.parametrize(
    ("model", "quality", "size", "expected"),
    [
        ("gpt-image-1.5", "low", "1024x1024", 0.009),
        ("gpt-image-1.5", "medium", "1536x1024", 0.050),
        ("gpt-image-1.5", "high", "1024x1536", 0.200),
        ("gpt-image-1-mini", "low", "1024x1024", 0.005),
        ("gpt-image-1-mini", "high", "1536x1024", 0.052),
    ],
)
def test_pricing_new_models(mock_b64_dalle, model, quality, size, expected):
    provider, _, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model=model,
        prompt="x",
        params={"quality": quality, "size": size},
    )
    result = provider.generate(step)
    assert result.cost_usd == pytest.approx(expected)


def test_gpt_image_2_pricing_none(mock_b64_dalle):
    """gpt-image-2 pricing undisclosed → cost_usd stays None."""
    provider, _, _ = mock_b64_dalle
    step = Step(
        provider="openai-dalle",
        model="gpt-image-2",
        prompt="x",
        params={"quality": "high", "size": "1024x1024"},
    )
    result = provider.generate(step)
    assert result.cost_usd is None


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
