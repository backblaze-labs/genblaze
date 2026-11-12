"""Tests for UberLLMProvider (mocked; no billable requests)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.request import url2pathname

import httpx
import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests
from genblaze_uberllm import UberLLMProvider

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(handler, tmp_path, **kwargs):
    return UberLLMProvider(
        api_key="test-key", http_client=_client(handler), output_dir=tmp_path, **kwargs
    )


def _image_response(**extra):
    return httpx.Response(
        200,
        json={
            "created": 1,
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
            "usage": {"cost_usd": 0.008},
            **extra,
        },
    )


def _path(asset_url: str) -> Path:
    return Path(url2pathname(asset_url.removeprefix("file://")))


def test_image_generation_saves_b64_output_with_cost(tmp_path):
    seen = {}

    def handler(request):
        seen["request"] = request
        return _image_response()

    provider = _provider(handler, tmp_path)
    step = Step(
        provider="uberllm",
        model="venice/z-image-turbo",
        modality=Modality.IMAGE,
        prompt="studio photo",
        params={"size": "1024x1024"},
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    request = seen["request"]
    assert str(request.url) == "https://api.uberllm.dev/v1/images/generations"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body == {
        "size": "1024x1024",
        "model": "venice/z-image-turbo",
        "prompt": "studio photo",
        "response_format": "b64_json",
    }
    asset = result.assets[0]
    assert asset.url.startswith("file://")
    assert asset.media_type == "image/png"
    assert _path(asset.url).read_bytes() == _PNG
    assert result.cost_usd == pytest.approx(0.008)


def test_image_with_source_picture_uses_edits_route(tmp_path):
    def handler(request):
        assert request.url.path == "/v1/images/edits"
        return _image_response()

    step = Step(
        provider="uberllm",
        model="google/nano-banana-pro-edit",
        modality=Modality.IMAGE,
        prompt="make the sky a sunset",
        params={"image": "https://example.com/skyline.png"},
    )
    assert _provider(handler, tmp_path).invoke(step).status == StepStatus.SUCCEEDED


def test_video_job_polls_then_downloads_artifact_with_key(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/v1/video/generations"
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if request.url.path == "/v1/video/generations/job-1":
            return httpx.Response(
                200,
                json={
                    "id": "job-1",
                    "status": "succeeded",
                    "model": "fal-ai/vidu/q3/text-to-video/turbo",
                    "results": [
                        {
                            "url": "https://api.uberllm.dev/v1/media/artifacts/job-1/0",
                            "content_type": "video/mp4",
                        }
                    ],
                    "usage": {"cost_usd": 0.04},
                },
            )
        assert request.url.path == "/v1/media/artifacts/job-1/0"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, content=_MP4, headers={"content-type": "video/mp4"})

    provider = _provider(handler, tmp_path, poll_interval=0)
    step = Step(
        provider="uberllm",
        model="fal-ai/vidu/q3/text-to-video/turbo",
        modality=Modality.VIDEO,
        prompt="a red ball bouncing",
        params={"duration_seconds": 1},
    )
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert result.assets[0].media_type == "video/mp4"
    assert _path(result.assets[0].url).read_bytes() == _MP4
    assert result.cost_usd == pytest.approx(0.04)
    assert calls.count(("GET", "/v1/video/generations/job-1")) == 1


def test_key_is_not_sent_to_other_hosts(tmp_path):
    def handler(request):
        if request.url.host != "api.uberllm.dev":
            pytest.fail("connector fetched a non-UberLLM host")
        return _image_response(data=[{"url": "https://cdn.example.com/out.png"}])

    step = Step(provider="uberllm", model="m", modality=Modality.IMAGE, prompt="p")
    result = _provider(handler, tmp_path).invoke(step)
    assert result.assets[0].url == "https://cdn.example.com/out.png"


def test_failed_video_job_reports_reason(tmp_path):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-2"})
        return httpx.Response(
            200, json={"id": "job-2", "status": "failed", "error": {"message": "no provider"}}
        )

    step = Step(provider="uberllm", model="m", modality=Modality.VIDEO, prompt="p")
    result = _provider(handler, tmp_path, poll_interval=0).invoke(step)
    assert result.status == StepStatus.FAILED
    assert "no provider" in (result.error or "")


def test_submit_is_single_attempt_on_transport_failure(tmp_path):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous POST timeout", request=request)

    step = Step(provider="uberllm", model="m", modality=Modality.VIDEO, prompt="p")
    result = _provider(handler, tmp_path).invoke(step)
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    assert attempts == 1


def test_invalid_key_maps_to_auth_failure_with_api_message(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    step = Step(provider="uberllm", model="m", modality=Modality.IMAGE, prompt="p")
    with pytest.raises(ProviderError, match="Invalid API key") as err:
        _provider(handler, tmp_path).submit(step)
    assert err.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_rejects_non_https_input_url_before_submit(tmp_path):
    provider = _provider(lambda request: pytest.fail("request should not be sent"), tmp_path)
    step = Step(
        provider="uberllm",
        model="edit-model",
        modality=Modality.IMAGE,
        params={"input_images": [{"url": "http://127.0.0.1/private.png", "role": "reference"}]},
    )
    with pytest.raises(ProviderError):
        provider.submit(step)


def test_missing_key_fails_before_request(tmp_path, monkeypatch):
    monkeypatch.delenv("UBERLLM_API_KEY", raising=False)
    provider = UberLLMProvider(http_client=_client(lambda request: None), output_dir=tmp_path)
    step = Step(provider="uberllm", model="m", modality=Modality.IMAGE)
    with pytest.raises(ProviderError, match="UBERLLM_API_KEY"):
        provider.submit(step)


class TestUberLLMCompliance(ProviderComplianceTests):
    def make_provider(self):
        def handler(request):
            return _image_response()

        return UberLLMProvider(api_key="test-key", http_client=_client(handler), poll_interval=0)

    def make_step(self):
        return Step(
            provider="uberllm",
            model="venice/z-image-turbo",
            modality=Modality.IMAGE,
            prompt="test prompt",
        )

    def test_full_lifecycle(self):
        result = self.make_provider().invoke(self.make_step())
        assert result.status == StepStatus.SUCCEEDED
