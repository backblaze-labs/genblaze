"""Tests for NvidiaVideoProvider."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SubmitResult
from genblaze_core.testing import ProviderComplianceTests

from .conftest import make_mock_http_client

_MP4_B64 = base64.b64encode(b"fake-mp4-bytes-for-test").decode()


@pytest.fixture
def tmp_output_dir(tmp_path):
    return tmp_path


@pytest.fixture
def provider(tmp_output_dir):
    from genblaze_nvidia import NvidiaVideoProvider

    p = NvidiaVideoProvider(api_key="nvapi-test", output_dir=tmp_output_dir, poll_interval=0.0)
    # Bypass lazy http(): inject a mock that returns 202 then a successful poll.
    p._client._http_client = make_mock_http_client(
        submit_status=202,
        submit_body={},
        submit_headers={"NVCF-REQID": "req-vid-001"},
        poll_statuses=[200],
        poll_body={"artifacts": [{"base64": _MP4_B64, "mime_type": "video/mp4"}]},
    )
    return p


# --- Submit ---


def test_submit_returns_submit_result_with_nvcf_id(provider):
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="a sunset",
    )
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)
    assert result.prediction_id == "req-vid-001"


def test_submit_forwards_prompt(provider):
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="a sunset",
    )
    provider.submit(step)
    body = provider._client._http_client.post.call_args.kwargs.get("json")
    assert body["prompt"] == "a sunset"


def test_submit_202_without_reqid_raises(provider):
    provider._client._http_client.post.return_value.headers = {"Content-Type": "application/json"}
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="t",
    )
    with pytest.raises(ProviderError, match="NVCF-REQID"):
        provider.submit(step)


def test_submit_200_inline_short_circuits_poll(provider, tmp_output_dir):
    """A 200-inline response must populate poll cache so poll() returns True on first tick."""
    provider._client._http_client = make_mock_http_client(
        submit_status=200,
        submit_body={"artifacts": [{"base64": _MP4_B64, "mime_type": "video/mp4"}]},
    )
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="sync",
    )
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)
    assert result.estimated_seconds == 0.0
    assert provider.poll(result.prediction_id) is True


def test_submit_4xx_classifies_error(provider):
    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.json.return_value = {"detail": "prompt blocked by safety filter"}
    err_resp.text = '{"detail": "prompt blocked by safety filter"}'
    err_resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = err_resp

    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="unsafe",
    )
    with pytest.raises(ProviderError) as info:
        provider.submit(step)
    from genblaze_core.models.enums import ProviderErrorCode

    # Safety markers win over the 400 status → CONTENT_POLICY, not INVALID_INPUT.
    assert info.value.error_code == ProviderErrorCode.CONTENT_POLICY
    # Error message must contain the clean detail string, not a Python repr
    # of the response dict (which would use single quotes and be ugly).
    msg = str(info.value)
    assert "prompt blocked by safety filter" in msg
    assert "{'detail':" not in msg, "Python repr leaked into error message"


# --- Poll ---


def test_poll_returns_false_on_202(provider):
    """NVCF still-running 202 → poll returns False."""
    poll_resp = MagicMock()
    poll_resp.status_code = 202
    poll_resp.json.return_value = {"status": "running"}
    poll_resp.text = "{}"
    poll_resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.get.return_value = poll_resp
    provider._client._http_client.get.side_effect = None

    assert provider.poll("req-vid-999") is False


def test_poll_returns_true_on_200(provider):
    assert provider.poll("req-vid-001") is True


# --- Fetch output ---


def test_fetch_output_writes_base64_to_file(provider, tmp_output_dir):
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="test",
    )
    provider.poll("req-vid-001")
    result = provider.fetch_output("req-vid-001", step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "video/mp4"
    assert asset.url.startswith("file://")
    # Bytes round-trip correctly from base64.
    path = Path(asset.url.removeprefix("file://"))
    assert path.read_bytes() == b"fake-mp4-bytes-for-test"


def test_fetch_output_hosted_url_preferred_over_base64(provider):
    """When a response has both a URL and base64, prefer the URL (no disk write)."""
    hosted_resp = MagicMock()
    hosted_resp.status_code = 200
    hosted_resp.json.return_value = {
        "artifacts": [
            {"url": "https://nvcf.example/cdn/video.mp4", "base64": _MP4_B64},
        ]
    }
    hosted_resp.text = "{}"
    hosted_resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.get.return_value = hosted_resp
    provider._client._http_client.get.side_effect = None

    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="test",
    )
    provider.poll("req-vid-777")
    result = provider.fetch_output("req-vid-777", step)
    assert len(result.assets) == 1
    assert result.assets[0].url == "https://nvcf.example/cdn/video.mp4"


def test_fetch_output_rejects_non_https_hosted_url(provider):
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.return_value = {"artifacts": [{"url": "http://insecure.example/video.mp4"}]}
    bad_resp.text = "{}"
    bad_resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.get.return_value = bad_resp
    provider._client._http_client.get.side_effect = None

    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="t",
    )
    provider.poll("req-vid-bad")
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("req-vid-bad", step)


def test_invoke_full_lifecycle(provider):
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="a sunset",
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_credentials_not_in_provider_payload(provider):
    provider.poll("req-vid-001")
    step = Step(
        provider="nvidia-video",
        model="nvidia/cosmos-1.0-7b-diffusion-text2world",
        prompt="t",
    )
    result = provider.fetch_output("req-vid-001", step)
    assert "nvapi-test" not in json.dumps(result.provider_payload)


# --- Pass-through for unknown models ---


def test_unknown_model_passthrough(provider):
    step = Step(
        provider="nvidia-video",
        model="some-vendor/unreleased-model-v99",
        prompt="test",
    )
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)


# --- Compliance ---


class TestNvidiaVideoCompliance(ProviderComplianceTests):
    # Pricing is intentionally None — NIM's free tier is RPM-gated and Cosmos
    # enterprise pricing is contract-specific. Users can attach pricing at
    # runtime via the registry; the default carries no cost table.
    expects_cost = False

    def make_provider(self):
        import tempfile

        from genblaze_nvidia import NvidiaVideoProvider

        p = NvidiaVideoProvider(
            api_key="nvapi-compliance",
            output_dir=Path(tempfile.mkdtemp()),
            poll_interval=0.0,
        )
        p._client._http_client = make_mock_http_client(
            submit_status=202,
            submit_body={},
            submit_headers={"NVCF-REQID": "req-compliance"},
            poll_statuses=[200],
            poll_body={"artifacts": [{"base64": _MP4_B64, "mime_type": "video/mp4"}]},
        )

        # Clear estimated_seconds so _attempt_once doesn't sleep.
        original_submit = p.submit

        def fast_submit(step, config=None):
            r = original_submit(step, config)
            if hasattr(r, "estimated_seconds"):
                r.estimated_seconds = None
            return r

        p.submit = fast_submit
        return p

    def make_step(self):
        return Step(
            provider="nvidia-video",
            model="nvidia/cosmos-1.0-7b-diffusion-text2world",
            prompt="test prompt",
        )
