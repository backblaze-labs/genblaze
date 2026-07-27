"""Tests for GeminiImageProvider (mocked — no real API calls).

Gemini-native image models return inline base64 bytes via
``generate_content`` (``candidates[].content.parts[].inline_data``), unlike
Imagen's ``:predict`` shape. See issue #205.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


def _candidate(parts, finish_reason="STOP"):
    """Build a mock generate_content candidate.

    ``parts`` is a list of (data, mime_type) tuples for inline-image parts;
    pass ``[]`` to model a response that carried no image (safety refusal or
    an empty result). ``finish_reason`` is a plain string because the
    provider normalizes it via ``str(reason).rsplit('.', 1)[-1]``.
    """
    mock_parts = []
    for data, mime in parts:
        inline = MagicMock()
        inline.data = data
        inline.mime_type = mime
        part = MagicMock()
        part.inline_data = inline
        mock_parts.append(part)
    candidate = MagicMock()
    candidate.content.parts = mock_parts
    candidate.finish_reason = finish_reason
    return candidate


def _response(candidates):
    resp = MagicMock()
    resp.candidates = candidates
    return resp


@pytest.fixture
def mock_gemini(tmp_path):
    """Patch google.genai and hand back a provider wired to a mock client.

    The client's ``generate_content`` defaults to a single-PNG happy
    response; individual tests override ``return_value``/``side_effect``.
    """
    mock_types = MagicMock()
    mock_genai = MagicMock()
    mock_genai.types = mock_types
    mock_google = MagicMock()
    mock_google.genai = mock_genai

    with patch.dict(
        "sys.modules",
        {"google": mock_google, "google.genai": mock_genai, "google.genai.types": mock_types},
    ):
        from genblaze_google.gemini_image import GeminiImageProvider

        client = MagicMock()
        client.models.generate_content.return_value = _response(
            [_candidate([(_PNG_BYTES, "image/png")])]
        )
        provider = GeminiImageProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = client
        yield provider, client


def _step(model="gemini-2.5-flash-image", prompt="a mountain lake at sunrise"):
    return Step(provider="google-gemini-image", model=model, prompt=prompt)


def test_generate_returns_image_asset(mock_gemini):
    provider, _ = mock_gemini
    result = provider.generate(_step())
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "image/png"
    assert asset.url.startswith("file://")


def test_generate_passes_prompt_to_generate_content(mock_gemini):
    provider, client = mock_gemini
    provider.generate(_step(prompt="a red bicycle"))
    client.models.generate_content.assert_called_once()
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash-image"
    # prompt is threaded into the contents payload
    assert "red bicycle" in str(kwargs["contents"])


def test_multiple_image_parts_yield_multiple_assets(mock_gemini):
    provider, client = mock_gemini
    client.models.generate_content.return_value = _response(
        [_candidate([(_PNG_BYTES, "image/png"), (_PNG_BYTES, "image/jpeg")])]
    )
    result = provider.generate(_step())
    assert [a.media_type for a in result.assets] == ["image/png", "image/jpeg"]


def test_no_candidates_raises_invalid_input(mock_gemini):
    provider, client = mock_gemini
    client.models.generate_content.return_value = _response([])
    with pytest.raises(ProviderError) as excinfo:
        provider.generate(_step())
    assert excinfo.value.error_code == ProviderErrorCode.INVALID_INPUT
    assert "no candidates" in str(excinfo.value)


def test_safety_refusal_raises_content_policy(mock_gemini):
    """A candidate with no inline image and a safety finish_reason is a
    deterministic content-policy refusal, not a generic empty result."""
    provider, client = mock_gemini
    client.models.generate_content.return_value = _response(
        [_candidate([], finish_reason="IMAGE_SAFETY")]
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.generate(_step(prompt="disallowed content"))
    assert excinfo.value.error_code == ProviderErrorCode.CONTENT_POLICY
    assert "safety" in str(excinfo.value).lower()


def test_empty_non_safety_raises_invalid_input(mock_gemini):
    """No inline image and a non-safety finish_reason is a plain empty
    result — INVALID_INPUT, not CONTENT_POLICY."""
    provider, client = mock_gemini
    client.models.generate_content.return_value = _response([_candidate([], finish_reason="STOP")])
    with pytest.raises(ProviderError) as excinfo:
        provider.generate(_step())
    assert excinfo.value.error_code == ProviderErrorCode.INVALID_INPUT
    assert "no inline image data" in str(excinfo.value)


def test_api_error_wrapped_as_provider_error(mock_gemini):
    provider, client = mock_gemini
    client.models.generate_content.side_effect = RuntimeError("503 backend unavailable")
    with pytest.raises(ProviderError, match="Gemini image generation failed"):
        provider.generate(_step())


def test_invoke_full_lifecycle_succeeds(mock_gemini):
    provider, _ = mock_gemini
    result = provider.invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


# --- Compliance harness ---


class TestGeminiImageCompliance(ProviderComplianceTests):
    """Verify GeminiImageProvider satisfies the genblaze provider contract."""

    expects_cost = False  # SDK no longer ships pricing as of genblaze-core 0.3.0.

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        mock_types = MagicMock()
        mock_genai = MagicMock()
        mock_google = MagicMock()
        mock_google.genai = mock_genai
        with patch.dict(
            "sys.modules",
            {
                "google": mock_google,
                "google.genai": mock_genai,
                "google.genai.types": mock_types,
            },
        ):
            yield

    def make_provider(self):
        from genblaze_google.gemini_image import GeminiImageProvider

        client = MagicMock()
        client.models.generate_content.return_value = _response(
            [_candidate([(_PNG_BYTES, "image/png")])]
        )
        provider = GeminiImageProvider(api_key="test-key", output_dir=tempfile.mkdtemp())
        provider._client = client
        return provider

    def make_step(self):
        return _step()
