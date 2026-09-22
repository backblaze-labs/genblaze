"""Tests for FalProvider (fully mocked; no live key, no billable requests)."""

from __future__ import annotations

import json
from importlib.metadata import entry_points

import httpx
import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers import RetryPolicy
from genblaze_core.testing import ProviderComplianceTests
from genblaze_fal import FalProvider
from genblaze_fal._errors import map_fal_error

_KEY = "fal-test-key-123"
_QUEUE = "https://queue.fal.run"
_REQ = "764cabcf-b745-4b3e-ae38-1200304cf45b"
# Self-describing prediction id: the request path relative to the queue host.
_PID = f"fal-ai/flux/requests/{_REQ}"
# Zero-delay core retry policy so retry tests run instantly.
_FAST_RETRY = RetryPolicy(max_attempts=3, initial_backoff_sec=0, max_backoff_sec=0, jitter="none")


def _urls(app: str = "fal-ai/flux", request_id: str = _REQ) -> dict[str, str]:
    """Submit-response URLs, shaped like fal's documented queue response."""
    base = f"{_QUEUE}/{app}/requests/{request_id}"
    return {"status_url": f"{base}/status", "response_url": base}


def _submit_body(request_id: str = _REQ, **overrides: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        **_urls(request_id=request_id),
        "queue_position": 0,
        **overrides,
    }


def _provider(handler, **kwargs) -> FalProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("poll_interval", 0)
    return FalProvider(api_key=_KEY, http_client=client, **kwargs)


def _step(model: str = "fal-ai/flux/schnell", **kwargs) -> Step:
    kwargs.setdefault("modality", Modality.IMAGE)
    kwargs.setdefault("prompt", "a sunset over mountains")
    return Step(provider="fal", model=model, **kwargs)


def _lifecycle_handler(
    result: dict,
    *,
    status: str = "COMPLETED",
    seen: list | None = None,
    status_extra: dict | None = None,
    submit_overrides: dict | None = None,
):
    """Route submit / status / response like the fal queue does."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=_submit_body(**(submit_overrides or {})))
        if request.url.path.endswith("/status"):
            body = {"status": status, "request_id": _REQ, "metrics": {"inference_time": 1.5}}
            return httpx.Response(200, json={**body, **(status_extra or {})})
        return httpx.Response(200, json=result)

    return handler


_IMAGE_RESULT = {
    "images": [
        {
            "url": "https://v3.fal.media/files/rabbit/abc123.png",
            "width": 1024,
            "height": 768,
            "content_type": "image/png",
        }
    ],
    "seed": 42,
    "has_nsfw_concepts": [False],
}


# --- submit ---------------------------------------------------------------


def test_submit_posts_to_queue_with_key_auth_and_returns_request_id():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_submit_body())

    step = _step(params={"image_size": "square_hd", "num_images": 1}, seed=7)
    assert _provider(handler).submit(step) == _PID

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{_QUEUE}/fal-ai/flux/schnell"
    assert request.headers["authorization"] == f"Key {_KEY}"
    body = json.loads(request.content)
    assert body == {
        "prompt": "a sunset over mountains",
        "seed": 7,
        "image_size": "square_hd",
        "num_images": 1,
    }
    assert _KEY.encode() not in request.content


def test_submit_is_single_attempt_on_ambiguous_transport_failure():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous POST timeout", request=request)

    result = _provider(handler).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    assert attempts == 1


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("ambiguous"),
        httpx.RemoteProtocolError("x"),
    ],
)
def test_core_retry_policy_never_resends_submit(error):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise error

    provider = _provider(handler, retry_policy=_FAST_RETRY)
    assert provider.invoke(_step()).status == StepStatus.FAILED
    assert attempts == 1


@pytest.mark.parametrize(
    "model",
    [
        "flux",  # no owner segment
        "fal-ai/../admin",
        "fal-ai/flux?fal_webhook=https://attacker.example/hook",
        "fal-ai/flux#frag",
        "https://attacker.example/fal-ai/flux",
        "/fal-ai/flux",
        "fal-ai//flux",
        "fal-ai/flux\n",
        "fal-ai/fl%75x",
        "user@fal-ai/flux",
    ],
)
def test_submit_rejects_unsafe_model_ids_before_request(model):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_step(model=model))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "params",
    [
        {"image_url": "http://127.0.0.1/private.png"},
        {"image_urls": ["https://ok.example/a.png", "http://169.254.169.254/latest"]},
        {"video_url": "file:///tmp/clip.mp4"},
        {"audio": "ftp://files.example/a.wav"},
        {"loras": [{"path_url": "http://169.254.169.254/latest"}]},
        {"reference_images": [{"image_url": "http://127.0.0.1/x.png"}]},
        {"image_urls": [["http://127.0.0.1/nested.png"]]},
        {"Image_URL": "http://127.0.0.1/mixed-case.png"},
    ],
)
def test_submit_rejects_unsafe_url_params_before_request(params):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_step(model="fal-ai/some/edit", params=params))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_submit_forwards_https_and_data_uri_inputs():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_submit_body())

    params = {
        "image_url": "https://v3.fal.media/files/in.png",
        "mask_url": "data:image/png;base64,iVBORw0KGgo=",
    }
    _provider(handler).submit(_step(model="fal-ai/some/edit", params=params))
    body = json.loads(seen[0].content)
    assert body["image_url"] == params["image_url"]
    assert body["mask_url"] == params["mask_url"]


def test_chain_input_routes_to_image_url_for_unknown_slug():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_submit_body())

    step = _step(
        model="fal-ai/some-owner/image-to-video",
        modality=Modality.VIDEO,
        inputs=[Asset(url="https://v3.fal.media/files/frame.png", media_type="image/png")],
    )
    _provider(handler).submit(step)
    assert json.loads(seen[0].content)["image_url"] == "https://v3.fal.media/files/frame.png"


@pytest.mark.parametrize("value", [True, "true", "1"])
def test_submit_rejects_sync_mode(value):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError, match="sync_mode") as info:
        provider.submit(_step(params={"sync_mode": value}))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_submit_allows_explicit_sync_mode_false():
    def handler(request):
        return httpx.Response(200, json=_submit_body())

    assert _provider(handler).submit(_step(params={"sync_mode": "false"})) == _PID


def test_submit_http_error_is_mapped_and_does_not_leak_key():
    def handler(request):
        return httpx.Response(401, json={"detail": "Invalid key"})

    with pytest.raises(ProviderError) as info:
        _provider(handler).submit(_step())
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE
    assert "Invalid key" in str(info.value)
    assert _KEY not in str(info.value)


@pytest.mark.parametrize("request_id", [None, "", "../../etc", "a/b"])
def test_submit_without_usable_request_id_fails(request_id):
    def handler(request):
        return httpx.Response(200, json={"request_id": request_id})

    with pytest.raises(ProviderError, match="request_id") as info:
        _provider(handler).submit(_step())
    assert info.value.error_code == ProviderErrorCode.SERVER_ERROR


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("fal-ai/flux/schnell", f"fal-ai/flux/requests/{_REQ}"),
        ("fal-ai/stable-audio", f"fal-ai/stable-audio/requests/{_REQ}"),
        ("workflows/me/my-app/run", f"workflows/me/my-app/requests/{_REQ}"),
    ],
)
def test_prediction_id_is_derived_when_response_url_is_absent(model, expected):
    def handler(request):
        return httpx.Response(200, json={"request_id": _REQ})

    assert _provider(handler).submit(_step(model=model)) == expected


def test_missing_key_fails_before_request(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no request")))
    provider = FalProvider(http_client=client)
    with pytest.raises(ProviderError, match="FAL_KEY") as info:
        provider.submit(_step())
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_key_read_from_fal_key_env(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "env-key")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_submit_body())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    FalProvider(http_client=client).submit(_step())
    assert seen[0].headers["authorization"] == "Key env-key"


# --- queue URL trust ------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"response_url": f"https://attacker.example/fal-ai/flux/requests/{_REQ}"},
        {"response_url": f"https://queue.fal.run.attacker.example/fal-ai/flux/requests/{_REQ}"},
        {"response_url": f"http://queue.fal.run/fal-ai/flux/requests/{_REQ}"},
        {"response_url": f"https://user@queue.fal.run/fal-ai/flux/requests/{_REQ}"},
        {"response_url": f"https://queue.fal.run:abc/fal-ai/flux/requests/{_REQ}"},
        {"response_url": f"https://queue.fal.run/evil/x/requests/{_REQ}?fal_webhook=https://e"},
        {"response_url": "https://queue.fal.run/fal-ai/flux/requests/some-other-id"},
        {"status_url": "https://attacker.example/steal/status"},
    ],
)
def test_untrusted_queue_urls_are_never_followed(overrides):
    hosts: list[str] = []
    provider = _provider(_lifecycle_handler(_IMAGE_RESULT, seen=None, submit_overrides=overrides))
    original = provider._client.send

    def recording_send(request, **kwargs):
        hosts.append(request.url.host)
        return original(request, **kwargs)

    provider._client.send = recording_send  # type: ignore[method-assign]
    result = provider.invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert set(hosts) == {"queue.fal.run"}
    assert result.provider_payload["fal"]["request_id"] == _REQ


# --- poll -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "done"),
    [("IN_QUEUE", False), ("IN_PROGRESS", False), ("COMPLETED", True)],
)
def test_poll_uses_status_url_and_reports_completion(status, done):
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(_IMAGE_RESULT, status=status, seen=seen))
    request_id = provider.submit(_step())
    assert provider.poll(request_id) is done
    assert str(seen[-1].url) == _urls()["status_url"]
    assert seen[-1].headers["authorization"] == f"Key {_KEY}"


def test_core_policy_retries_transient_poll_failures_then_succeeds():
    status_calls = 0

    def handler(request):
        nonlocal status_calls
        if request.method == "POST":
            return httpx.Response(200, json=_submit_body())
        if not request.url.path.endswith("/status"):
            return httpx.Response(200, json=_IMAGE_RESULT)
        status_calls += 1
        if status_calls == 1:
            raise httpx.ConnectError("reset", request=request)
        if status_calls == 2:
            return httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"status": "COMPLETED", "request_id": _REQ})

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert status_calls == 3


def test_poll_retries_are_bounded_by_the_policy():
    status_calls = 0

    def handler(request):
        nonlocal status_calls
        if request.method == "POST":
            return httpx.Response(200, json=_submit_body())
        status_calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    assert status_calls == 3


def test_poll_does_not_retry_client_errors():
    status_calls = 0

    def handler(request):
        nonlocal status_calls
        if request.method == "POST":
            return httpx.Response(200, json=_submit_body())
        status_calls += 1
        return httpx.Response(401, json={"detail": "revoked"})

    provider = _provider(handler)
    with pytest.raises(ProviderError) as info:
        provider.poll(provider.submit(_step()))
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE
    assert status_calls == 1


@pytest.mark.parametrize(
    "prediction_id",
    [
        _REQ,  # bare request id: no app path
        "../../admin/requests/x",
        "https://attacker.example/fal-ai/flux/requests/x",
        "fal-ai/flux/requests/x?fal_webhook=https://e",
        "fal-ai/flux/status/x",
    ],
)
def test_malformed_prediction_id_is_rejected_without_request(prediction_id):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError, match="prediction id") as info:
        provider.poll(prediction_id)
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_resume_from_checkpoint_works_on_a_fresh_instance():
    submitted: list[str] = []
    _provider(_lifecycle_handler(_IMAGE_RESULT)).invoke(
        _step(), {"on_submit": lambda _step_id, pid: submitted.append(pid)}
    )
    assert submitted == [_PID]

    # A new process: fresh provider, only the checkpointed id survives.
    seen: list[httpx.Request] = []
    fresh = _provider(_lifecycle_handler(_IMAGE_RESULT, seen=seen))
    result = fresh.resume(submitted[0], _step())
    assert result.status == StepStatus.SUCCEEDED
    assert [str(r.url) for r in seen] == [_urls()["status_url"], _urls()["response_url"]]
    # Resuming again (e.g. after a downstream sink failure) still works.
    assert fresh.resume(submitted[0], _step()).status == StepStatus.SUCCEEDED


# --- fetch_output ---------------------------------------------------------


def test_invoke_maps_image_output_to_asset():
    seen: list[httpx.Request] = []
    result = _provider(_lifecycle_handler(_IMAGE_RESULT, seen=seen)).invoke(_step())

    assert result.status == StepStatus.SUCCEEDED
    (asset,) = result.assets
    assert asset.url == "https://v3.fal.media/files/rabbit/abc123.png"
    assert asset.media_type == "image/png"
    assert (asset.width, asset.height) == (1024, 768)
    assert str(seen[-1].url) == _urls()["response_url"]
    assert result.provider_payload["fal"] == {
        "request_id": _REQ,
        "inference_time": 1.5,
        "seed": 42,
    }


def test_invoke_maps_video_output_to_asset():
    video = {"video": {"url": "https://v3.fal.media/files/out.mp4"}, "seed": 1}
    step = _step(model="fal-ai/wan/v2.2-a14b/text-to-video", modality=Modality.VIDEO)
    result = _provider(_lifecycle_handler(video)).invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert [(a.url, a.media_type) for a in result.assets] == [
        ("https://v3.fal.media/files/out.mp4", "video/mp4")
    ]


def test_invoke_maps_audio_file_output_with_audio_metadata():
    audio = {
        "audio_file": {
            "url": "https://v3.fal.media/files/loop.wav",
            "content_type": "audio/wav",
            "file_size": 4404019,
        }
    }
    step = _step(model="fal-ai/stable-audio", modality=Modality.AUDIO)
    result = _provider(_lifecycle_handler(audio)).invoke(step)
    (asset,) = result.assets
    assert asset.media_type == "audio/wav"
    assert asset.size_bytes == 4404019
    assert isinstance(asset.audio, AudioMetadata)


def test_bare_audio_url_output_infers_media_type_from_key_and_extension():
    audio = {"audio_url": "https://v3.fal.media/files/speech.mp3"}
    # Default step modality is IMAGE: the output key must win over it.
    result = _provider(_lifecycle_handler(audio)).invoke(_step(model="fal-ai/some/tts"))
    (asset,) = result.assets
    assert asset.media_type == "audio/mpeg"
    assert asset.audio is not None


@pytest.mark.parametrize(
    "url", ["http://v3.fal.media/files/x.png", "data:image/png;base64,iVBORw0KGgo="]
)
def test_fetch_output_rejects_unhosted_output_url(url):
    bad = {"images": [{"url": url}]}
    result = _provider(_lifecycle_handler(bad)).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.MODEL_ERROR
    assert result.assets == []


def test_extension_beats_modality_for_unknown_output_keys():
    video = {
        "video": {"url": "https://v3.fal.media/files/out.mp4"},
        "thumbnail": {"url": "https://v3.fal.media/files/thumb.png"},
    }
    step = _step(model="fal-ai/some/video-model", modality=Modality.VIDEO)
    result = _provider(_lifecycle_handler(video)).invoke(step)
    assert [a.media_type for a in result.assets] == ["video/mp4", "image/png"]


def test_malformed_status_metrics_do_not_fail_a_completed_job():
    handler = _lifecycle_handler(_IMAGE_RESULT, status_extra={"metrics": ["not", "a", "dict"]})
    result = _provider(handler).invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert "inference_time" not in result.provider_payload["fal"]


def test_fetch_output_without_media_is_model_error():
    result = _provider(_lifecycle_handler({"seed": 1})).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.MODEL_ERROR


def test_failed_generation_is_classified_from_response_error():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=_submit_body())
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "COMPLETED", "request_id": _REQ})
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "prompt"],
                        "msg": "The content could not be processed",
                        "type": "content_policy_violation",
                        "url": "https://docs.fal.ai/errors#content_policy_violation",
                    }
                ]
            },
        )

    result = _provider(handler).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.CONTENT_POLICY
    assert "content_policy_violation" in (result.error or "")


# --- redaction ------------------------------------------------------------


def test_key_never_reaches_step_or_manifest_payload():
    result = _provider(_lifecycle_handler(_IMAGE_RESULT)).invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert _KEY not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status_extra", "code"),
    [
        (
            {"error": "Content flagged", "error_type": "content_policy_violation"},
            ProviderErrorCode.CONTENT_POLICY,
        ),
        # Terminal failures are never retryable: fal already re-queued the runner.
        (
            {"error": "Runner died", "error_type": "runner_disconnected"},
            ProviderErrorCode.MODEL_ERROR,
        ),
        (
            {"error": "Request timed out", "error_type": "request_timeout"},
            ProviderErrorCode.MODEL_ERROR,
        ),
        ({"error": "Generation failed"}, ProviderErrorCode.MODEL_ERROR),
    ],
)
def test_status_error_is_classified_without_reading_the_result(status_extra, code):
    seen: list[httpx.Request] = []
    handler = _lifecycle_handler(_IMAGE_RESULT, seen=seen, status_extra=status_extra)
    result = _provider(handler).invoke(_step(), {"max_retries": 2})
    assert result.status == StepStatus.FAILED
    assert result.error_code == code
    assert result.assets == []
    assert status_extra.get("error_type", status_extra["error"]) in (result.error or "")
    # Only submit + one status read: the stored error response is never fetched.
    assert [r.url.path.endswith("/status") for r in seen[1:]] == [True]


def test_close_only_closes_an_internal_client():
    injected = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    FalProvider(api_key=_KEY, http_client=injected).close()
    assert not injected.is_closed

    owned = FalProvider(api_key=_KEY)
    owned.close()
    assert owned._client.is_closed


# --- error mapping --------------------------------------------------------


def _status_error(status: int, body: object = None, headers: dict | None = None):
    request = httpx.Request("GET", f"{_QUEUE}/x")
    response = httpx.Response(status, json=body, headers=headers, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("t"), ProviderErrorCode.TIMEOUT),
        (_status_error(401), ProviderErrorCode.AUTH_FAILURE),
        (_status_error(403), ProviderErrorCode.AUTH_FAILURE),
        (_status_error(404), ProviderErrorCode.MODEL_ERROR),
        (_status_error(429), ProviderErrorCode.RATE_LIMIT),
        (_status_error(400), ProviderErrorCode.INVALID_INPUT),
        (
            _status_error(422, {"detail": [{"type": "value_error", "msg": "bad"}]}),
            ProviderErrorCode.INVALID_INPUT,
        ),
        (
            _status_error(422, {"detail": [{"type": "content_policy_violation"}]}),
            ProviderErrorCode.CONTENT_POLICY,
        ),
        (
            _status_error(422, {"detail": [{"type": "no_media_generated"}]}),
            ProviderErrorCode.MODEL_ERROR,
        ),
        (
            _status_error(504, {"detail": "Request timed out", "error_type": "request_timeout"}),
            ProviderErrorCode.TIMEOUT,
        ),
        (
            _status_error(503, None, {"X-Fal-Error-Type": "runner_disconnected"}),
            ProviderErrorCode.SERVER_ERROR,
        ),
        (_status_error(500), ProviderErrorCode.SERVER_ERROR),
        (ValueError("something odd"), ProviderErrorCode.UNKNOWN),
    ],
)
def test_map_fal_error(exc, code):
    assert map_fal_error(exc) == code


# --- construction, registry, discovery ------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://queue.fal.run",
        "https://queue.fal.run?fal_webhook=https://attacker.example",
        "https://queue.fal.run#frag",
        "https://user:pw@queue.fal.run",
        "https://queue.fal.run/prefix",
        "queue.fal.run",
    ],
)
def test_rejects_unsafe_base_url(base_url):
    with pytest.raises(ValueError, match="base_url"):
        FalProvider(api_key=_KEY, base_url=base_url)


def test_starter_registry_lists_known_models():
    known = FalProvider(api_key=_KEY).models.known()
    for slug in (
        "fal-ai/flux/schnell",
        "fal-ai/wan/v2.2-a14b/text-to-video",
        "fal-ai/stable-audio",
    ):
        assert slug in known


@pytest.mark.parametrize(
    ("model", "modality"),
    [
        ("fal-ai/flux/dev", Modality.IMAGE),
        ("fal-ai/wan/v2.2-a14b/image-to-video", Modality.VIDEO),
        ("fal-ai/stable-audio", Modality.AUDIO),
    ],
)
def test_registry_families_declare_modality(model, modality):
    assert FalProvider(api_key=_KEY).models.get(model).modality == modality


def test_stable_audio_maps_standard_duration_param():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_submit_body())

    step = _step(model="fal-ai/stable-audio", modality=Modality.AUDIO, params={"duration": 12})
    _provider(handler).submit(step)
    body = json.loads(seen[0].content)
    assert body["seconds_total"] == 12
    assert "duration" not in body


def test_unknown_slug_passes_params_through():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_submit_body())

    step = _step(model="fal-ai/brand-new/model", params={"custom_knob": 3})
    _provider(handler).submit(step)
    assert str(seen[0].url) == f"{_QUEUE}/fal-ai/brand-new/model"
    assert json.loads(seen[0].content)["custom_knob"] == 3


def test_entry_point_resolves_to_provider():
    (ep,) = [ep for ep in entry_points(group="genblaze.providers") if ep.name == "fal"]
    assert ep.load() is FalProvider


# --- compliance harness ---------------------------------------------------


class TestFalCompliance(ProviderComplianceTests):
    # fal bills per model-specific unit (megapixels, video seconds, compute
    # seconds); the starter registry does not ship pricing, so cost_usd stays
    # unset rather than reporting a wrong number.
    expects_cost = False

    def make_provider(self):
        return _provider(_lifecycle_handler(_IMAGE_RESULT))

    def make_step(self):
        return _step()

    def constructor_kwargs_for_probe_cache_test(self):
        return {"api_key": _KEY}


class TestFalAudioCompliance(TestFalCompliance):
    """Re-run the harness on an audio step so the AudioMetadata check is exercised."""

    def make_provider(self):
        audio = {
            "audio_file": {"url": "https://v3.fal.media/files/a.wav", "content_type": "audio/wav"}
        }
        return _provider(_lifecycle_handler(audio))

    def make_step(self):
        return _step(model="fal-ai/stable-audio", modality=Modality.AUDIO)
