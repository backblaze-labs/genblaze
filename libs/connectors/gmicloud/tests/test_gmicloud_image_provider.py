"""Tests for GMICloudImageProvider (mocked — no real API calls)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SubmitResult
from genblaze_core.testing import ProviderComplianceTests

from .conftest import make_mock_http_client


@pytest.fixture
def provider():
    from genblaze_gmicloud.image import GMICloudImageProvider

    p = GMICloudImageProvider(api_key="test-api-key-123")
    p._http_client = make_mock_http_client(
        request_id="req-img-001",
        outcome_url="https://gmicloud-output.com/result.png",
    )
    return p


# --- Submit ---


def test_submit_returns_submit_result(provider):
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)
    assert result.prediction_id == "req-img-001"


def test_submit_forwards_params(provider):
    step = Step(
        provider="gmicloud-image",
        model="seedream-5.0-lite",
        prompt="test",
        params={"aspect_ratio": "16:9"},
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["aspect_ratio"] == "16:9"


def test_submit_forwards_negative_prompt_from_step_field(provider):
    """Pipeline hoists negative_prompt out of params onto the Step field."""
    step = Step(
        provider="gmicloud-image",
        model="seedream-5.0-lite",
        prompt="a cat",
        negative_prompt="blurry, watermark",
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["negative_prompt"] == "blurry, watermark"


def test_submit_edit_model_with_image_input(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-image",
        model="reve-edit-fast-20251030",
        prompt="make it brighter",
        inputs=[Asset(url="https://example.com/photo.jpg", media_type="image/jpeg")],
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["image"] == "https://example.com/photo.jpg"


# --- Poll ---


def test_poll_returns_true_on_success(provider):
    assert provider.poll("req-img-001") is True


def test_poll_returns_false_on_processing(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "processing"}
    provider._http_client.get.return_value = resp
    assert provider.poll("req-img-001") is False


# --- Fetch output ---


def test_fetch_output_attaches_image_asset(provider):
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "image/png"


def test_fetch_output_infers_jpeg_mime(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"media_urls": [{"url": "https://gmicloud-output.com/result.jpeg"}]},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.assets[0].media_type == "image/jpeg"


def test_fetch_output_legacy_image_url_fallback(provider):
    """GMI legacy flat `image_url` shape still parses — defensive compat."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"image_url": "https://gmicloud-output.com/legacy.png"},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.assets[0].url == "https://gmicloud-output.com/legacy.png"


def test_fetch_output_thumbnail_image_fallback(provider):
    """When media_urls is absent, fall back to outcome.thumbnail_image_url for images."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"thumbnail_image_url": "https://gmicloud-output.com/thumb.png"},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.assets[0].url == "https://gmicloud-output.com/thumb.png"


def test_fetch_output_failed_raises(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "failed", "error": "Safety filter triggered"}
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="bad")
    with pytest.raises(ProviderError, match="Safety filter"):
        provider.fetch_output("req-img-001", step)


def test_invoke_full_lifecycle(provider):
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


# --- Cost ---


def test_cost_none_by_default(provider):
    """SDK no longer ships pricing for GMICloud as of genblaze-core 0.3.0."""
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.cost_usd is None


def test_cost_tracked_with_user_registered_pricing(provider):
    """User-registered per-unit pricing flows through compute_cost."""
    from genblaze_core.providers import per_unit

    # Fork before mutating so the test doesn't pollute the class-level
    # models_default() cache (and therefore other tests).
    provider._models = provider.models.fork()
    provider.models.register_pricing("seedream-5.0-lite", per_unit(0.035))
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.cost_usd == pytest.approx(0.035)


def test_cost_none_unknown_model(provider):
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="unknown-model", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.cost_usd is None


# --- Payload + security ---


def test_provider_payload_populated(provider):
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.provider_payload["gmicloud"]["request_id"] == "req-img-001"


def test_credentials_not_in_provider_payload(provider):
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert "test-api-key-123" not in json.dumps(result.provider_payload)


def test_asset_url_rejects_non_https(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"media_urls": [{"url": "file:///etc/passwd"}]},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="test")
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("req-img-001", step)


def test_chain_input_rejects_http_url(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-image",
        model="reve-edit-fast-20251030",
        prompt="edit",
        inputs=[Asset(url="http://evil.com/payload.bin", media_type="image/png")],
    )
    with pytest.raises(ProviderError, match="[Uu]nsafe"):
        provider.submit(step)


# --- Passthrough ---


def test_unknown_model_passthrough(provider):
    step = Step(provider="gmicloud-image", model="NewImageModel-v99", prompt="test")
    assert isinstance(provider.submit(step), SubmitResult)


# --- Deprecated aliases ---


def test_legacy_pascalcase_slug_passes_through_permissive_fallback(provider):
    """Soft-launch clean break (genblaze-core 0.3.0): the SDK no longer
    registers per-slug PascalCase ``deprecated_aliases``. Legacy
    PascalCase ids still execute (the request goes to the wire as-is)
    but no DeprecationWarning is emitted — there's nothing to redirect
    them to. Users should pass canonical lowercase slugs.
    """
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    # Step still completes; pricing is None unless the user registered it.
    assert result.assets, "fetch_output should produce an asset"


# --- Multi-image output ---


def test_fetch_output_emits_asset_per_media_url(provider):
    """Model returning N URLs in the envelope must produce N assets (not 1)."""
    provider._http_client = make_mock_http_client(
        request_id="req-img-001",
        outcome_urls=[
            "https://gmicloud-output.com/img-0.png",
            "https://gmicloud-output.com/img-1.png",
            "https://gmicloud-output.com/img-2.png",
        ],
    )
    step = Step(
        provider="gmicloud-image",
        model="seedream-5.0-lite",
        prompt="a cat",
        params={"number_of_images": 3},
    )
    provider.poll("req-img-001")
    result = provider.fetch_output("req-img-001", step)
    assert len(result.assets) == 3
    assert [a.url for a in result.assets] == [
        "https://gmicloud-output.com/img-0.png",
        "https://gmicloud-output.com/img-1.png",
        "https://gmicloud-output.com/img-2.png",
    ]


def test_fetch_output_is_atomic_on_validation_failure(provider):
    """If one URL fails validation, no partial assets land on the step."""
    provider._http_client = make_mock_http_client(
        request_id="req-img-001",
        outcome_urls=[
            "https://gmicloud-output.com/img-0.png",
            "http://insecure.example/img-1.png",  # HTTPS required — will reject
            "https://gmicloud-output.com/img-2.png",
        ],
    )
    step = Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="a cat")
    provider.poll("req-img-001")
    with pytest.raises(ProviderError):
        provider.fetch_output("req-img-001", step)
    # The valid first URL must NOT have landed on the step — atomic failure.
    assert step.assets == []


# --- Compliance ---


class TestGMICloudImageCompliance(ProviderComplianceTests):
    # SDK no longer ships pricing for GMICloud (genblaze-core 0.3.0).
    expects_cost = False

    def make_provider(self):
        from genblaze_gmicloud.image import GMICloudImageProvider

        p = GMICloudImageProvider(api_key="test-compliance-key")
        p._http_client = make_mock_http_client(
            outcome_url="https://gmicloud-output.com/result.png",
        )
        p.poll_interval = 0.0

        original_submit = p.submit

        def fast_submit(step, config=None):
            r = original_submit(step, config)
            r.estimated_seconds = None
            return r

        p.submit = fast_submit
        return p

    def make_step(self):
        return Step(provider="gmicloud-image", model="seedream-5.0-lite", prompt="test prompt")
