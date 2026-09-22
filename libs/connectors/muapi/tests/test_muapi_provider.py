"""MuAPI provider tests; all HTTP calls are synthetic and non-billable."""

from __future__ import annotations

import json
from importlib.metadata import entry_points

import httpx
import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.providers import ValidationOutcome
from genblaze_core.testing import ProviderComplianceTests
from genblaze_muapi import MuAPIProvider

_KEY = "muapi-test-key-123"
_BASE = "https://api.muapi.test/api/v1"
_REQUEST_ID = "3d4f0f2b-9b1e-4ec8-8fb6-a5c5c0f8d7e2"
_IMAGE_URL = "https://cdn.muapi.test/outputs/image.png"


def _step(
    model: str = "flux-schnell",
    *,
    modality: Modality = Modality.IMAGE,
    params: dict | None = None,
):
    from genblaze_core.models.step import Step

    return Step(
        provider="muapi",
        model=model,
        modality=modality,
        prompt="a sunset over mountains",
        params=params or {},
    )


def _provider(handler, **kwargs) -> MuAPIProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("api_key", _KEY)
    kwargs.setdefault("base_url", _BASE)
    kwargs.setdefault("poll_interval", 0)
    kwargs.setdefault("http_client", client)
    return MuAPIProvider(**kwargs)


def _result(
    *,
    status: str = "completed",
    outputs: list | None = None,
    **extra,
) -> dict:
    return {
        "request_id": _REQUEST_ID,
        "status": status,
        "outputs": outputs if outputs is not None else [{"url": _IMAGE_URL}],
        **extra,
    }


def _lifecycle_handler(result: dict, *, seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": _REQUEST_ID, "status": "processing"})
        return httpx.Response(200, json=result)

    return handler


def test_submit_uses_catalog_slug_and_x_api_key_without_putting_key_in_body():
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(_result(), seen=seen))
    request_id = provider.submit(_step(params={"resolution": "1024x1024", "steps": 4}))

    assert request_id == _REQUEST_ID
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{_BASE}/flux-schnell"
    assert request.headers["x-api-key"] == _KEY
    body = json.loads(request.content)
    assert body == {
        "prompt": "a sunset over mountains",
        "resolution": "1024x1024",
        "steps": 4,
    }
    assert _KEY.encode() not in request.content


def test_chain_input_routes_to_image_url():
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(_result(), seen=seen))
    from genblaze_core.models.asset import Asset

    step = _step()
    step.inputs = [Asset(url="https://cdn.example/input.png", media_type="image/png")]
    provider.submit(step)
    assert json.loads(seen[0].content)["image_url"] == "https://cdn.example/input.png"


@pytest.mark.parametrize(
    "model",
    ["../admin", "flux/schnell", "https://evil.test/model", "flux?x=1", "flux model"],
)
def test_unsafe_model_ids_are_rejected_before_network(model):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_step(model=model))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "params",
    [
        {"image_url": "http://127.0.0.1/private.png"},
        {"reference_images": [{"image_url": "file:///tmp/private.png"}]},
        {"video_urls": ["ftp://example.test/clip.mp4"]},
    ],
)
def test_url_bearing_params_reject_non_hosted_inputs(params):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_step(params=params))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_data_uri_input_is_forwarded():
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(_result(), seen=seen))
    provider.submit(_step(params={"image_url": "data:image/png;base64,AAAA"}))
    assert json.loads(seen[0].content)["image_url"].startswith("data:image/png")


def test_missing_key_fails_before_submit():
    provider = _provider(lambda request: pytest.fail("request must not be sent"), api_key=None)
    provider._api_key = None
    with pytest.raises(ProviderError) as info:
        provider.submit(_step())
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_invoke_maps_image_output_cost_and_metadata():
    seen: list[httpx.Request] = []
    result = _result(
        outputs=[{"url": _IMAGE_URL, "width": 1024, "height": 768}],
        cost={"amount_usd": 0.04},
    )
    response = _provider(_lifecycle_handler(result, seen=seen)).invoke(_step())

    assert response.status == StepStatus.SUCCEEDED
    (asset,) = response.assets
    assert asset.url == _IMAGE_URL
    assert asset.media_type == "image/png"
    assert (asset.width, asset.height) == (1024, 768)
    assert response.cost_usd == 0.04
    assert response.provider_payload == {"muapi": {"request_id": _REQUEST_ID, "cost_usd": 0.04}}
    assert [request.method for request in seen] == ["POST", "GET"]


def test_audio_output_gets_audio_metadata_and_extension_type():
    result = _result(outputs=[{"url": "https://cdn.muapi.test/outputs/audio.wav"}])
    response = _provider(
        _lifecycle_handler(result),
    ).invoke(_step(model="minimax-music-3.0", modality=Modality.AUDIO))
    (asset,) = response.assets
    assert asset.media_type.startswith("audio/")
    assert isinstance(asset.audio, AudioMetadata)


def test_failed_detail_envelope_is_classified_without_fetching_outputs():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": _REQUEST_ID})
        return httpx.Response(
            200,
            json={"detail": {"status": "failed", "error": "content policy violation"}},
        )

    response = _provider(handler).invoke(_step())
    assert response.status == StepStatus.FAILED
    assert response.error_code == ProviderErrorCode.CONTENT_POLICY
    assert len(seen) == 2


@pytest.mark.parametrize("url", ["http://cdn.muapi.test/image.png", "data:image/png;base64,AAAA"])
def test_unhosted_output_url_is_rejected(url):
    response = _provider(_lifecycle_handler(_result(outputs=[url]))).invoke(_step())
    assert response.status == StepStatus.FAILED
    assert response.error_code == ProviderErrorCode.MODEL_ERROR
    assert response.assets == []


def test_completed_job_without_outputs_is_model_error():
    response = _provider(_lifecycle_handler(_result(outputs=[]))).invoke(_step())
    assert response.status == StepStatus.FAILED
    assert response.error_code == ProviderErrorCode.MODEL_ERROR


def test_invalid_prediction_id_is_rejected_without_network():
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.poll("../../etc/passwd")
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_resume_from_checkpoint_works_on_a_fresh_provider():
    submitted: list[str] = []
    _provider(_lifecycle_handler(_result())).invoke(
        _step(), {"on_submit": lambda _step_id, value: submitted.append(value)}
    )
    fresh = _provider(_lifecycle_handler(_result()))
    response = fresh.resume(submitted[0], _step())
    assert response.status == StepStatus.SUCCEEDED
    assert response.assets[0].url == _IMAGE_URL


def test_native_discovery_filters_to_enabled_media_catalog():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "flux-schnell", "category": "Text to Image", "domain": "ai"},
                    {
                        "name": "seedance-2.5-text-to-video",
                        "category": "Text to Video",
                        "domain": "ai",
                    },
                    {"name": "minimax-music-3.0", "category": "Text to Audio", "domain": "ai"},
                    {"name": "gpt-5", "category": "Text to Text", "domain": "ai"},
                    {"name": "disabled-image", "category": "Text to Image", "is_enabled": False},
                    {"name": "coming-soon", "category": "Text to Image", "is_coming_soon": True},
                    {"name": "seo-keyword-research", "category": "Text to Text", "domain": "seo"},
                ]
            },
        )

    provider = _provider(handler)
    discovery = provider.discover_models()
    assert discovery.slugs == {
        "flux-schnell",
        "seedance-2.5-text-to-video",
        "minimax-music-3.0",
    }
    assert provider.validate_model("flux-schnell").outcome is ValidationOutcome.OK_AUTHORITATIVE
    assert provider.validate_model("gpt-5").outcome is ValidationOutcome.NOT_FOUND


def test_discovery_failure_is_non_fatal_and_reported():
    provider = _provider(lambda request: httpx.Response(503, json={"error": "offline"}))
    discovery = provider.discover_models()
    assert discovery.status.value == "failed"
    assert discovery.slugs == frozenset()


def test_entry_point_resolves_to_provider():
    (ep,) = [ep for ep in entry_points(group="genblaze.providers") if ep.name == "muapi"]
    assert ep.load() is MuAPIProvider


class TestMuAPICompliance(ProviderComplianceTests):
    def make_provider(self):
        return _provider(_lifecycle_handler(_result(cost={"amount_usd": 0.01})))

    def make_step(self):
        return _step()

    def constructor_kwargs_for_probe_cache_test(self):
        return {"api_key": _KEY, "base_url": _BASE}


class TestMuAPIAudioCompliance(TestMuAPICompliance):
    def make_step(self):
        return _step(model="minimax-music-3.0", modality=Modality.AUDIO)

    def make_provider(self):
        return _provider(
            _lifecycle_handler(
                _result(
                    outputs=[{"url": "https://cdn.muapi.test/outputs/audio.mp3"}],
                    cost={"amount_usd": 0.02},
                )
            )
        )
