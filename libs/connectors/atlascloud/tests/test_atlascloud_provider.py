"""Tests for AtlasCloudProvider (mocked; no billable requests)."""

from __future__ import annotations

import httpx
import pytest
from genblaze_atlascloud import AtlasCloudProvider
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(handler, **kwargs):
    return AtlasCloudProvider(api_key="test-key", http_client=_client(handler), **kwargs)


def test_image_submit_uses_image_endpoint_and_does_not_leak_key():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"code": 200, "data": {"id": "pred-1"}})

    provider = _provider(handler)
    step = Step(
        provider="atlascloud",
        model="bytedance/seedream-v5.0-lite",
        modality=Modality.IMAGE,
        prompt="studio photo",
        params={"size": "2048*2048"},
    )
    assert provider.submit(step) == "pred-1"
    request = seen["request"]
    assert request.url.path.endswith("/generateImage")
    assert request.headers["authorization"] == "Bearer test-key"
    assert b"test-key" not in request.content


def test_video_submit_uses_video_endpoint():
    def handler(request):
        assert request.url.path.endswith("/generateVideo")
        return httpx.Response(200, json={"data": {"id": "pred-2"}})

    step = Step(
        provider="atlascloud",
        model="bytedance/seedance-2.0/text-to-video",
        modality=Modality.VIDEO,
        prompt="ocean waves",
    )
    assert _provider(handler).submit(step) == "pred-2"


def test_submit_is_single_attempt_on_transport_failure():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous POST timeout", request=request)

    provider = _provider(handler)
    step = Step(provider="atlascloud", model="image-model", modality=Modality.IMAGE)
    result = provider.invoke(step)
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    assert attempts == 1


def test_poll_and_fetch_output_share_terminal_response():
    requests = 0

    def handler(request):
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "id": "pred-3",
                    "status": "completed",
                    "outputs": ["https://cdn.atlascloud.ai/result.png"],
                },
            },
        )

    provider = _provider(handler)
    assert provider.poll("pred-3") is True
    step = Step(provider="atlascloud", model="image-model", modality=Modality.IMAGE)
    result = provider.fetch_output("pred-3", step)
    assert requests == 1
    assert result.assets[0].url == "https://cdn.atlascloud.ai/result.png"
    assert result.assets[0].media_type == "image/jpeg"


def test_rejects_non_https_input_url_before_submit():
    provider = _provider(lambda request: pytest.fail("request should not be sent"))
    step = Step(
        provider="atlascloud",
        model="edit-model",
        modality=Modality.IMAGE,
        params={"image_url": "http://127.0.0.1/private.png"},
    )
    with pytest.raises(ProviderError):
        provider.submit(step)


def test_missing_key_fails_before_request():
    provider = AtlasCloudProvider(api_key="", http_client=_client(lambda request: None))
    provider._api_key = None
    step = Step(provider="atlascloud", model="image-model", modality=Modality.IMAGE)
    with pytest.raises(ProviderError, match="ATLASCLOUD_API_KEY"):
        provider.submit(step)


class TestAtlasCloudCompliance(ProviderComplianceTests):
    expects_cost = False

    def make_provider(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"data": {"id": "pred-ok"}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "pred-ok",
                        "status": "completed",
                        "outputs": ["https://cdn.atlascloud.ai/result.png"],
                    }
                },
            )

        return _provider(handler, poll_interval=0)

    def make_step(self):
        return Step(
            provider="atlascloud",
            model="bytedance/seedream-v5.0-lite",
            modality=Modality.IMAGE,
            prompt="test prompt",
        )

    def test_full_lifecycle(self):
        result = self.make_provider().invoke(self.make_step())
        assert result.status == StepStatus.SUCCEEDED
