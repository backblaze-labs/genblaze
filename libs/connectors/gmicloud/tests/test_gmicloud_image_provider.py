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
        outcome_key="image_url",
        outcome_url="https://gmicloud-output.com/result.png",
    )
    return p


# --- Submit ---


def test_submit_returns_submit_result(provider):
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)
    assert result.prediction_id == "req-img-001"


def test_submit_forwards_params(provider):
    step = Step(
        provider="gmicloud-image",
        model="Seedream-5.0-Lite",
        prompt="test",
        params={"aspect_ratio": "16:9"},
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["aspect_ratio"] == "16:9"


def test_submit_edit_model_with_image_input(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-image",
        model="Reve-Edit-Fast",
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
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "image/png"


def test_fetch_output_infers_jpeg_mime(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"image_url": "https://gmicloud-output.com/result.jpeg"},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.assets[0].media_type == "image/jpeg"


def test_fetch_output_failed_raises(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "failed", "error": "Safety filter triggered"}
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="bad")
    with pytest.raises(ProviderError, match="Safety filter"):
        provider.fetch_output("req-img-001", step)


def test_invoke_full_lifecycle(provider):
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


# --- Cost ---


def test_cost_tracked(provider):
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
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
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert result.provider_payload["gmicloud"]["request_id"] == "req-img-001"


def test_credentials_not_in_provider_payload(provider):
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="a cat")
    result = provider.fetch_output("req-img-001", step)
    assert "test-api-key-123" not in json.dumps(result.provider_payload)


def test_asset_url_rejects_non_https(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "success", "outcome": {"image_url": "file:///etc/passwd"}}
    provider._http_client.get.return_value = resp
    provider.poll("req-img-001")
    step = Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="test")
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("req-img-001", step)


def test_chain_input_rejects_http_url(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud-image",
        model="Reve-Edit-Fast",
        prompt="edit",
        inputs=[Asset(url="http://evil.com/payload.bin", media_type="image/png")],
    )
    with pytest.raises(ProviderError, match="[Uu]nsafe"):
        provider.submit(step)


# --- Passthrough ---


def test_unknown_model_passthrough(provider):
    step = Step(provider="gmicloud-image", model="NewImageModel-v99", prompt="test")
    assert isinstance(provider.submit(step), SubmitResult)


# --- Compliance ---


class TestGMICloudImageCompliance(ProviderComplianceTests):
    def make_provider(self):
        from genblaze_gmicloud.image import GMICloudImageProvider

        p = GMICloudImageProvider(api_key="test-compliance-key")
        p._http_client = make_mock_http_client(
            outcome_key="image_url",
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
        return Step(provider="gmicloud-image", model="Seedream-5.0-Lite", prompt="test prompt")
