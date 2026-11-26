"""Tests for AnexProvider (fully mocked; no live key, no billable requests).

Response bodies follow the documented shapes at https://anex.sh/docs/video.
The task, upscale and error shapes were checked against the live API on
2026-11-25.
"""

from __future__ import annotations

import json
from importlib.metadata import entry_points

import httpx
import pytest
from genblaze_anex import (
    DEFAULT_BASE_URL,
    DEFAULT_CATALOG_URL,
    EU_BASE_URL,
    AnexProvider,
    CatalogRates,
)
from genblaze_anex import provider as provider_module
from genblaze_anex._catalog import parse_catalog
from genblaze_anex._errors import map_anex_error, map_anex_task_error
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus, StepType
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoveryStatus,
    ModelSpec,
    RetryPolicy,
    ValidationOutcome,
    ValidationSource,
)
from genblaze_core.providers.pricing import PricingContext
from genblaze_core.testing import ProviderComplianceTests

_KEY = "anex-test-key-123"
_TASK_ID = "318407566120873452"
_CLIP_URL = "https://media.example.com/clips/318407566120873452.mp4?sig=abc"
_CREATE_URL = f"{DEFAULT_BASE_URL}/v2/video_generation"
_UPSCALE_URL = f"{DEFAULT_BASE_URL}/v2/video_regeneration"
_TASK_URL = f"{DEFAULT_BASE_URL}/v2/query/video_generation/{_TASK_ID}"
# Zero-delay core retry policy so retry tests run instantly.
_FAST_RETRY = RetryPolicy(max_attempts=3, initial_backoff_sec=0, max_backoff_sec=0, jitter="none")


def _task(status: str = "succeeded", **overrides) -> dict:
    """A task object, shaped like the documented query response."""
    task = {
        "id": _TASK_ID,
        "model": "MiniMax-H3",
        "status": status,
        "created_at": 1761750000,
        "updated_at": 1761750210,
        "resolution": "768P",
        "duration": 6,
        "ratio": "16:9",
        "task_type": "generation",
        "modality": "video",
    }
    if status == "succeeded":
        task["content"] = {"url": _CLIP_URL}
        task["usage"] = {
            "total_seconds": 6,
            "input_seconds": 0,
            "output_seconds": 6,
            "input_image_count": 0,
        }
    task.update(overrides)
    return task


def _error_body(error_type: str, message: str, status: int) -> dict:
    return {
        "type": "error",
        "error": {"type": error_type, "message": message, "http_code": str(status)},
        "request_id": "req-1",
    }


def _video_entry(
    entry_id: str,
    names: list[str],
    kinds: list[str],
    resolutions: list[str],
    *,
    seed: bool,
    fees: dict | None = None,
) -> dict:
    """One video model, shaped like an entry of the public catalog."""
    return {
        "id": entry_id,
        "label": entry_id,
        "task": "generate_video",
        "max_output_tokens": 0,
        "media_fees": {"image_usd": None, **(fees or {})},
        "video": {
            "name": names[0],
            "names": names,
            "kinds": kinds,
            "resolutions": resolutions,
            "supports_seed": seed,
        },
        # A block the connector does not read: unknown fields are ignored.
        "clip": {"default_seconds": 6, "seconds": {"768P": [4, 15]}},
    }


_ALL = ["768P", "2K", "4K"]
_H3_FEES = {
    "video_output_usd_per_second": "0.068",
    "video_upscale_usd_per_second": "0.042",
    "video_upscale_4k_usd_per_second": "0.068",
    "video_input_image_usd": "0.034",
    "video_free_images": 5,
}


def _catalog(*extra: dict, drop: tuple[str, ...] = ()) -> dict:
    """The catalog answer of 2026-11-26, trimmed to the fields that matter."""
    entries = [
        # Other tasks share the list and are skipped.
        {"id": "qwen3.8-27b", "task": "generate", "media_fees": {}},
        _video_entry(
            "fasth3-8-step-v2",
            ["FastH3-8-Step-V2"],
            ["text"],
            ["768P"],
            seed=True,
            fees={"video_output_usd_per_second": "0.017", "video_free_images": None},
        ),
        _video_entry(
            "minimax-h3", ["MiniMax-H3"], ["text", "frames"], _ALL, seed=False, fees=_H3_FEES
        ),
        _video_entry(
            "minimax-h3-ref2va",
            ["MiniMax-H3", "MiniMax-H3-Ref2VA"],
            ["reference"],
            _ALL,
            seed=True,
            fees=_H3_FEES,
        ),
        _video_entry(
            "minimax-h3-ref2va-smd-4step",
            ["MiniMax-H3-Ref2VA-SMD-4step"],
            ["reference"],
            _ALL,
            seed=True,
            fees={
                **_H3_FEES,
                "video_output_usd_per_second": "0.051",
                "video_upscale_usd_per_second": "0.059",
            },
        ),
        {
            "id": "seedvr2-3b",
            "task": "upscale_video",
            "media_fees": {
                "video_output_usd_per_second": "0.0425",
                "video_output_4k_usd_per_second": "0.068",
            },
        },
        *extra,
    ]
    kept = [e for e in entries if not (isinstance(e, dict) and e.get("id") in drop)]
    return {"models": kept, "defaults": {"rows": 1}}


def _catalog_client(answer, seen: list | None = None) -> httpx.Client:
    """A client for the catalog host: ``answer`` is a JSON body or a response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer)

    return httpx.Client(transport=httpx.MockTransport(handler))


_UNREADABLE = httpx.Response(503, text="unavailable")


def _provider(
    handler, *, catalog=None, catalog_seen: list | None = None, **kwargs
) -> AnexProvider:
    """A provider on mocked transports. ``catalog`` defaults to today's answer."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("poll_interval", 0)
    kwargs.setdefault(
        "catalog_client", _catalog_client(_catalog() if catalog is None else catalog, catalog_seen)
    )
    return AnexProvider(api_key=_KEY, http_client=client, **kwargs)


def _step(model: str = "MiniMax-H3", **kwargs) -> Step:
    kwargs.setdefault("modality", Modality.VIDEO)
    kwargs.setdefault("prompt", "A red fox trots across fresh snow at dawn.")
    kwargs.setdefault("params", {"duration": 6})
    return Step(provider="anex", model=model, **kwargs)


def _lifecycle_handler(task: dict | None = None, *, seen: list | None = None):
    """Route create / query like the task API does."""
    task = _task() if task is None else task

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": _TASK_ID})
        return httpx.Response(200, json={"task": task})

    return handler


def _submitted_body(step: Step, **provider_kwargs) -> dict:
    """Submit a step and return the JSON body the provider sent."""
    seen: list[httpx.Request] = []
    assert _provider(_lifecycle_handler(seen=seen), **provider_kwargs).submit(step) == _TASK_ID
    return json.loads(seen[0].content)


# --- submit ---------------------------------------------------------------


def test_submit_posts_text_to_video_with_bearer_auth():
    seen: list[httpx.Request] = []
    step = _step(params={"duration": 6, "aspect_ratio": "16:9"}, seed=42)
    assert _provider(_lifecycle_handler(seen=seen)).submit(step) == _TASK_ID

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == _CREATE_URL
    assert request.headers["authorization"] == f"Bearer {_KEY}"
    assert _KEY.encode() not in request.content
    assert json.loads(request.content) == {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "A red fox trots across fresh snow at dawn."}],
        "resolution": "768P",
        "duration": 6,
        "ratio": "16:9",
        "seed": 42,
    }


@pytest.mark.parametrize(
    ("given", "sent"), [("768p", "768P"), ("2k", "2K"), ("4k", "4K"), ("2K", "2K")]
)
def test_resolution_is_sent_in_the_api_spelling(given, sent):
    body = _submitted_body(_step(params={"duration": 5, "resolution": given}))
    assert body["resolution"] == sent


def test_first_and_last_frame_params_become_content_items():
    body = _submitted_body(
        _step(
            params={
                "duration": 5,
                "image_url": "https://cdn.example.com/first.png",
                "end_image_url": "data:image/png;base64,AAAA",
            }
        )
    )
    assert body["content"][1:] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.example.com/first.png"},
            "role": "first_frame",
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "role": "last_frame",
        },
    ]
    assert "image_url" not in body and "end_image_url" not in body


def test_chained_images_become_first_and_last_frame():
    step = _step(
        inputs=[
            Asset(url="https://cdn.example.com/a.png", media_type="image/png"),
            Asset(url="https://cdn.example.com/b.jpg", media_type="image/jpeg"),
        ]
    )
    roles = [
        (item["role"], item["image_url"]["url"]) for item in _submitted_body(step)["content"][1:]
    ]
    assert roles == [
        ("first_frame", "https://cdn.example.com/a.png"),
        ("last_frame", "https://cdn.example.com/b.jpg"),
    ]


@pytest.mark.parametrize("catalog", [None, _UNREADABLE], ids=["catalog", "families"])
def test_minimax_h3_sends_other_chained_inputs_as_references(catalog):
    """A MiniMax-H3 request with a reference is rendered by the reference model."""
    video = [Asset(url="https://cdn.example.com/a.mp4", media_type="video/mp4")]
    body = _submitted_body(_step(inputs=video), catalog=catalog)
    assert [item["role"] for item in body["content"][1:]] == ["reference_video"]

    images = [
        Asset(url=f"https://cdn.example.com/{i}.png", media_type="image/png") for i in range(3)
    ]
    body = _submitted_body(_step(inputs=images), catalog=catalog)
    assert [item["role"] for item in body["content"][1:]] == ["reference_image"] * 3


def test_frames_only_model_rejects_inputs_it_cannot_use():
    frames_only = _video_entry("frames-1", ["Frames-1"], ["text", "frames"], ["768P"], seed=True)
    provider = _provider(
        lambda request: pytest.fail("request must not be sent"), catalog=_catalog(frames_only)
    )
    video = [Asset(url="https://cdn.example.com/a.mp4", media_type="video/mp4")]
    with pytest.raises(ProviderError, match="first and last frame") as info:
        provider.submit(_step(model="Frames-1", inputs=video))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


@pytest.mark.parametrize("model", ["MiniMax-H3-Ref2VA", "MiniMax-H3-Ref2VA-SMD-4step"])
def test_reference_model_routes_chained_inputs_by_media_type(model):
    step = _step(
        model=model,
        inputs=[
            Asset(url="https://cdn.example.com/person.png", media_type="image/png"),
            Asset(url="https://cdn.example.com/clip.mp4", media_type="video/mp4"),
            Asset(url="https://cdn.example.com/voice.wav", media_type="audio/wav"),
            Asset(url="https://cdn.example.com/place.webp", media_type="image/webp"),
        ],
    )
    body = _submitted_body(step)
    assert body["model"] == model
    assert [(item["type"], item["role"]) for item in body["content"][1:]] == [
        ("image_url", "reference_image"),
        ("image_url", "reference_image"),
        ("video_url", "reference_video"),
        ("audio_url", "reference_audio"),
    ]
    assert body["content"][3] == {
        "type": "video_url",
        "video_url": {"url": "https://cdn.example.com/clip.mp4"},
        "role": "reference_video",
    }


def test_reference_params_accept_a_single_url_and_uploads():
    body = _submitted_body(
        _step(
            model="MiniMax-H3-Ref2VA",
            params={
                "duration": 5,
                "reference_image_urls": "input:abc123",
                "reference_audio_urls": ["https://cdn.example.com/voice.mp3"],
            },
        )
    )
    assert body["content"][1:] == [
        {"type": "image_url", "image_url": {"url": "input:abc123"}, "role": "reference_image"},
        {
            "type": "audio_url",
            "audio_url": {"url": "https://cdn.example.com/voice.mp3"},
            "role": "reference_audio",
        },
    ]


@pytest.mark.parametrize("catalog", [None, _UNREADABLE], ids=["catalog", "families"])
def test_text_only_model_rejects_inputs_and_higher_resolutions(catalog):
    provider = _provider(lambda request: pytest.fail("request must not be sent"), catalog=catalog)
    image = Asset(url="https://cdn.example.com/a.png", media_type="image/png")
    with pytest.raises(ProviderError, match="text to video only"):
        provider.submit(_step(model="FastH3-8-Step-V2", inputs=[image]))
    with pytest.raises(ProviderError, match="resolution"):
        provider.submit(
            _step(model="FastH3-8-Step-V2", params={"duration": 5, "resolution": "2K"})
        )


@pytest.mark.parametrize(
    "params",
    [
        {"duration": 5, "image_url": "http://127.0.0.1/private.png"},
        {"duration": 5, "image_url": "file:///tmp/frame.png"},
        {"duration": 5, "end_image_url": "ftp://example.com/frame.png"},
        {"duration": 5, "image_url": 7},
    ],
)
def test_submit_rejects_unsafe_media_params_before_request(params):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_step(params=params))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"duration": 3},
        {"duration": 16},
        {"duration": 5.5},
        {"duration": 5, "resolution": "8K"},
    ],
)
def test_submit_validates_duration_and_resolution_before_request(params):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_step(params=params))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_whole_float_duration_is_sent_as_an_integer():
    body = _submitted_body(_step(params={"duration": 6.0}))
    assert body["duration"] == 6 and isinstance(body["duration"], int)


def test_submit_requires_a_prompt_and_refuses_a_negative_prompt():
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError, match="needs a prompt"):
        provider.submit(_step(prompt=None))
    with pytest.raises(ProviderError, match="negative prompt"):
        provider.submit(_step(negative_prompt="blurry"))


def test_unknown_model_and_params_pass_through():
    body = _submitted_body(
        _step(model="Some-Future-Model", params={"duration": 5, "callback_url": "https://e.x/cb"})
    )
    assert body["model"] == "Some-Future-Model"
    assert body["callback_url"] == "https://e.x/cb"


def test_submit_is_single_attempt_on_ambiguous_transport_failure():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous POST timeout", request=request)

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    assert attempts == 1


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_core_retry_policy_never_resends_submit(error):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise error("no connection", request=request)

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert attempts == 1


def test_submit_http_error_is_mapped_and_does_not_leak_key():
    def handler(request):
        body = _error_body("rate_limit_error", "30 requests in flight (1045)", 429)
        return httpx.Response(429, json=body, headers={"Retry-After": "7"})

    with pytest.raises(ProviderError, match="30 requests in flight") as info:
        _provider(handler).submit(_step())
    assert info.value.error_code == ProviderErrorCode.RATE_LIMIT
    assert info.value.retry_after == 7
    assert _KEY not in str(info.value)


@pytest.mark.parametrize("task_id", [None, "", "../admin", "a/b", "x?y=1", True])
def test_submit_without_usable_task_id_fails(task_id):
    def handler(request):
        return httpx.Response(200, json={"task_id": task_id})

    with pytest.raises(ProviderError, match="task_id") as info:
        _provider(handler).submit(_step())
    assert info.value.error_code == ProviderErrorCode.SERVER_ERROR


def test_missing_key_fails_before_request(monkeypatch):
    monkeypatch.delenv("ANEX_API_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("no request")))
    provider = AnexProvider(http_client=client, catalog_url=None)
    with pytest.raises(ProviderError, match="ANEX_API_KEY") as info:
        provider.submit(_step())
    assert info.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_key_read_from_env(monkeypatch):
    monkeypatch.setenv("ANEX_API_KEY", "env-key")
    seen: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(_lifecycle_handler(seen=seen)))
    AnexProvider(http_client=client, catalog_url=None).submit(_step())
    assert seen[0].headers["authorization"] == "Bearer env-key"


# --- regions and base_url ---------------------------------------------------


def test_eu_base_url_receives_every_request():
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(seen=seen), base_url=EU_BASE_URL)
    assert provider.invoke(_step()).status == StepStatus.SUCCEEDED
    assert EU_BASE_URL == "https://tasks-eu.anex.sh"
    assert [str(r.url) for r in seen] == [
        f"{EU_BASE_URL}/v2/video_generation",
        f"{EU_BASE_URL}/v2/query/video_generation/{_TASK_ID}",
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://tasks.anex.sh",
        "https://tasks.anex.sh/v2",
        "https://user:pw@tasks.anex.sh",
        "https://tasks.anex.sh?x=1",
        "https://tasks.anex.sh#frag",
        "https://tasks.anex.sh:0",
        "https://",
        "tasks.anex.sh",
    ],
)
def test_rejects_unsafe_base_url(base_url):
    with pytest.raises(ValueError, match="base_url"):
        AnexProvider(api_key=_KEY, base_url=base_url)


def test_misdirected_region_error_keeps_the_message():
    message = "this organization's data is kept in the EU region; use https://tasks-eu.anex.sh"

    def handler(request):
        return httpx.Response(421, json=_error_body("bad_request_error", message, 421))

    with pytest.raises(ProviderError, match="tasks-eu.anex.sh") as info:
        _provider(handler).submit(_step())
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


# --- upscale ----------------------------------------------------------------


def _upscale_step(**kwargs) -> Step:
    kwargs.setdefault("params", {"resolution": "4K", "source_task_id": _TASK_ID})
    return Step(
        provider="anex",
        model="MiniMax-H3",
        modality=Modality.VIDEO,
        step_type=StepType.UPSCALE,
        **kwargs,
    )


def _upscale_task(resolution: str = "2K", seconds: int = 4) -> dict:
    """A finished upscale, shaped like a real regeneration task (no image count)."""
    return _task(
        task_type="regeneration",
        resolution=resolution,
        usage={"total_seconds": seconds, "input_seconds": 0, "output_seconds": seconds},
    )


def test_upscale_by_task_id_posts_to_the_regeneration_endpoint():
    seen: list[httpx.Request] = []
    assert _provider(_lifecycle_handler(seen=seen)).submit(_upscale_step()) == _TASK_ID
    assert str(seen[0].url) == _UPSCALE_URL
    assert json.loads(seen[0].content) == {
        "model": "MiniMax-H3",
        "resolution": "4K",
        "source_task_id": _TASK_ID,
    }


def test_upscale_of_a_chained_clip_sends_it_as_the_base_video():
    seen: list[httpx.Request] = []
    step = _upscale_step(
        params={"resolution": "2k"},
        inputs=[Asset(url="https://cdn.example.com/clip.mp4", media_type="video/mp4")],
    )
    _provider(_lifecycle_handler(seen=seen)).submit(step)
    assert json.loads(seen[0].content) == {
        "model": "MiniMax-H3",
        "resolution": "2K",
        "content": [
            {
                "type": "video_url",
                "video_url": {"url": "https://cdn.example.com/clip.mp4"},
                "role": "base_video",
            }
        ],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"params": {"resolution": "768P", "source_task_id": _TASK_ID}},
        {"params": {"source_task_id": _TASK_ID}},
        {"params": {"resolution": "2K"}},
        {"params": {"resolution": "2K", "source_task_id": "../x"}},
        {
            "params": {
                "resolution": "2K",
                "source_task_id": _TASK_ID,
                "video_url": "https://cdn.example.com/clip.mp4",
            }
        },
        {"params": {"resolution": "2K", "video_url": "file:///tmp/clip.mp4"}},
        {
            "params": {"resolution": "2K"},
            "inputs": [
                Asset(url="https://cdn.example.com/a.mp4", media_type="video/mp4"),
                Asset(url="https://cdn.example.com/b.mp4", media_type="video/mp4"),
            ],
        },
    ],
)
def test_upscale_rejects_bad_requests_before_sending(kwargs):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError) as info:
        provider.submit(_upscale_step(**kwargs))
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_upscaled_asset_makes_no_size_or_audio_claim():
    task = _upscale_task("4K")
    result = _provider(_lifecycle_handler(task)).invoke(_upscale_step())
    assert result.status == StepStatus.SUCCEEDED
    asset = result.assets[0]
    assert (asset.width, asset.height) == (None, None)
    assert asset.video.has_audio is None
    assert asset.video.resolution == "4K"
    assert result.provider_payload["anex"]["task_type"] == "regeneration"


# --- poll -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "done"),
    [
        ("queued", False),
        ("running", False),
        ("succeeded", True),
        ("failed", True),
        ("cancelled", True),
    ],
)
def test_poll_reads_the_task_and_reports_completion(status, done):
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(_task(status), seen=seen))
    assert provider.poll(_TASK_ID) is done
    assert [(r.method, str(r.url)) for r in seen] == [("GET", _TASK_URL)]


def test_poll_and_fetch_output_share_the_terminal_read():
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(seen=seen))
    assert provider.poll(_TASK_ID) is True
    result = provider.fetch_output(_TASK_ID, _step())
    assert len(seen) == 1
    assert result.assets[0].url == _CLIP_URL


def test_core_policy_retries_transient_poll_failures_then_succeeds():
    reads = 0

    def handler(request):
        nonlocal reads
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": _TASK_ID})
        reads += 1
        if reads == 1:
            return httpx.Response(503, json=_error_body("server_error", "try again (1000)", 503))
        if reads == 2:
            raise httpx.ReadTimeout("slow read", request=request)
        return httpx.Response(200, json={"task": _task()})

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert reads == 3


def test_poll_does_not_retry_client_errors():
    reads = 0

    def handler(request):
        nonlocal reads
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": _TASK_ID})
        reads += 1
        return httpx.Response(404, json=_error_body("not_found_error", "unknown task (2013)", 404))

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.INVALID_INPUT
    assert reads == 1


@pytest.mark.parametrize(
    "prediction_id",
    ["", "../admin", "a/b", "123?filter.status=failed", "https://attacker.example/x", "a" * 200],
)
def test_malformed_prediction_id_is_rejected_without_request(prediction_id):
    provider = _provider(lambda request: pytest.fail("request must not be sent"))
    with pytest.raises(ProviderError, match="prediction id") as info:
        provider.poll(prediction_id)
    assert info.value.error_code == ProviderErrorCode.INVALID_INPUT


def test_task_read_without_a_task_object_is_a_server_error():
    provider = _provider(lambda request: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(ProviderError, match="task object") as info:
        provider.poll(_TASK_ID)
    assert info.value.error_code == ProviderErrorCode.SERVER_ERROR


def test_resume_from_checkpoint_works_on_a_fresh_instance():
    submitted: list[str] = []
    _provider(_lifecycle_handler()).invoke(
        _step(), {"on_submit": lambda _step_id, pid: submitted.append(pid)}
    )
    assert submitted == [_TASK_ID]

    # A new process: fresh provider, only the checkpointed id survives.
    seen: list[httpx.Request] = []
    fresh = _provider(_lifecycle_handler(seen=seen))
    result = fresh.resume(submitted[0], _step())
    assert result.status == StepStatus.SUCCEEDED
    assert [(r.method, str(r.url)) for r in seen] == [("GET", _TASK_URL)]


# --- fetch_output -----------------------------------------------------------


def test_invoke_maps_the_clip_to_a_video_asset():
    result = _provider(_lifecycle_handler()).invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    (asset,) = result.assets
    assert asset.url == _CLIP_URL
    assert asset.media_type == "video/mp4"
    assert (asset.width, asset.height) == (1344, 768)
    assert asset.video.frame_rate == 24.0
    assert asset.video.has_audio is True
    assert asset.video.resolution == "768P"
    assert result.provider_payload == {
        "anex": {
            "task_id": _TASK_ID,
            "status": "succeeded",
            "task_type": "generation",
            "resolution": "768P",
            "ratio": "16:9",
            "duration": 6,
            "usage": {
                "total_seconds": 6,
                "input_seconds": 0,
                "output_seconds": 6,
                "input_image_count": 0,
            },
        }
    }
    # No prices ship with the connector.
    assert result.cost_usd is None


@pytest.mark.parametrize(
    ("ratio", "resolution", "size"),
    [
        ("9:16", "2K", (1440, 2560)),
        ("21:9", "4K", (4464, 1872)),
        ("adaptive", "768P", (None, None)),
    ],
)
def test_asset_size_follows_the_documented_table(ratio, resolution, size):
    task = _task(ratio=ratio, resolution=resolution)
    result = _provider(_lifecycle_handler(task)).invoke(_step())
    assert (result.assets[0].width, result.assets[0].height) == size


def test_key_never_reaches_step_or_manifest_payload():
    result = _provider(_lifecycle_handler()).invoke(_step())
    assert _KEY not in result.model_dump_json()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            {"code": "content_policy_violation", "message": "refused by screening"},
            ProviderErrorCode.CONTENT_POLICY,
        ),
        (
            {"code": "media_fetch_failed", "message": "reference image 1 could not be fetched"},
            ProviderErrorCode.INVALID_INPUT,
        ),
        ({"code": "internal_error", "message": "render failed"}, ProviderErrorCode.MODEL_ERROR),
        (None, ProviderErrorCode.MODEL_ERROR),
    ],
)
def test_failed_task_is_classified_from_its_error_code(error, code):
    reads = 0

    def handler(request):
        nonlocal reads
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": _TASK_ID})
        reads += 1
        return httpx.Response(200, json={"task": _task("failed", error=error)})

    result = _provider(handler, retry_policy=_FAST_RETRY).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == code
    assert _TASK_ID in result.error
    # A failed task is terminal: it is read once and never retried.
    assert reads == 1


def test_cancelled_task_fails_the_step():
    result = _provider(_lifecycle_handler(_task("cancelled"))).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert "cancelled" in result.error


def test_fetch_output_on_an_unfinished_task_does_not_invent_an_asset():
    provider = _provider(_lifecycle_handler(_task("running")))
    with pytest.raises(ProviderError, match="has not finished"):
        provider.fetch_output(_TASK_ID, _step())


@pytest.mark.parametrize(
    "content", [None, {}, {"url": ""}, {"url": "http://insecure.example/c.mp4"}]
)
def test_succeeded_task_without_a_usable_link_is_a_model_error(content):
    provider = _provider(_lifecycle_handler(_task(content=content)))
    with pytest.raises(ProviderError) as info:
        provider.fetch_output(_TASK_ID, _step())
    assert info.value.error_code == ProviderErrorCode.MODEL_ERROR


def test_malformed_usage_does_not_fail_a_succeeded_task():
    task = _task(usage={"total_seconds": "six", "output_seconds": True, "input_seconds": 2})
    result = _provider(_lifecycle_handler(task)).invoke(_step())
    assert result.status == StepStatus.SUCCEEDED
    assert result.provider_payload["anex"]["usage"] == {"input_seconds": 2}


# --- cost -------------------------------------------------------------------


# The recipe documented in docs/reference/pricing-recipes.md: the rates come
# from the catalog, so no price is written here or in the connector.
def _anex_pricing(rates: CatalogRates):
    def cost(ctx: PricingContext) -> float | None:
        task = ctx.provider_payload.get("anex", {})
        usage = task.get("usage", {})
        model = ctx.step.model
        resolution = (
            task.get("resolution") or str(ctx.step.params.get("resolution", "768P")).upper()
        )
        if ctx.step.step_type == StepType.UPSCALE:
            rate = rates.upscale_per_second.get(resolution)
        else:
            rate = rates.per_second.get(model, {}).get(resolution)
        seconds = usage.get("total_seconds", ctx.step.params.get("duration"))
        if rate is None or seconds is None:
            return None
        extra_images = max(usage.get("input_image_count", 0) - rates.free_images.get(model, 0), 0)
        image_usd = rates.image_usd.get(model)
        if extra_images and image_usd is None:
            return None
        return seconds * rate + extra_images * (image_usd or 0)

    return cost


def _priced(provider: AnexProvider) -> AnexProvider:
    rates = provider.catalog_rates()
    assert rates is not None
    for model in rates.per_second:
        provider.models.register_pricing(model, _anex_pricing(rates))
    return provider


def test_catalog_rates_are_the_published_per_second_prices():
    rates = _provider(_lifecycle_handler()).catalog_rates()
    assert rates.per_second == {
        "MiniMax-H3": {"768P": 0.068, "2K": 0.11, "4K": 0.136},
        "MiniMax-H3-Ref2VA": {"768P": 0.068, "2K": 0.11, "4K": 0.136},
        "MiniMax-H3-Ref2VA-SMD-4step": {"768P": 0.051, "2K": 0.11, "4K": 0.119},
        "FastH3-8-Step-V2": {"768P": 0.017},
    }
    assert rates.upscale_per_second == {"2K": 0.0425, "4K": 0.068}
    assert rates.free_images["MiniMax-H3"] == 5 and rates.free_images["FastH3-8-Step-V2"] == 0
    assert rates.image_usd["MiniMax-H3-Ref2VA"] == 0.034
    assert "FastH3-8-Step-V2" not in rates.image_usd


def test_catalog_rates_follow_a_price_change_and_skip_what_is_unclear():
    changed = _catalog()
    by_id = {entry["id"]: entry for entry in changed["models"]}
    by_id["fasth3-8-step-v2"]["media_fees"]["video_output_usd_per_second"] = "0.02"
    # Two entries share the name MiniMax-H3; once they disagree, no rate is offered.
    by_id["minimax-h3-ref2va"]["media_fees"]["video_output_usd_per_second"] = "0.07"
    by_id["minimax-h3-ref2va-smd-4step"]["media_fees"]["video_output_usd_per_second"] = "free"
    rates = _provider(_lifecycle_handler(), catalog=changed).catalog_rates()
    assert rates.per_second["FastH3-8-Step-V2"] == {"768P": 0.02}
    assert "MiniMax-H3" not in rates.per_second
    assert rates.per_second["MiniMax-H3-Ref2VA"]["768P"] == 0.07
    assert "MiniMax-H3-Ref2VA-SMD-4step" not in rates.per_second


def test_catalog_rates_are_none_when_the_catalog_cannot_be_read():
    assert _provider(_lifecycle_handler(), catalog=_UNREADABLE).catalog_rates() is None
    assert AnexProvider(api_key=_KEY, catalog_url=None).catalog_rates() is None


@pytest.mark.parametrize(
    ("model", "task", "cost"),
    [
        ("MiniMax-H3", {}, 0.408),
        ("MiniMax-H3", {"resolution": "2K"}, 0.66),
        ("MiniMax-H3", {"resolution": "4K"}, 0.816),
        ("FastH3-8-Step-V2", {}, 0.102),
        (
            "MiniMax-H3-Ref2VA-SMD-4step",
            # A 6 s clip with 4 s of reference video and 7 images: 10 s and 2 paid images.
            {
                "usage": {
                    "total_seconds": 10,
                    "input_seconds": 4,
                    "output_seconds": 6,
                    "input_image_count": 7,
                }
            },
            0.578,
        ),
    ],
)
def test_registered_pricing_fills_cost_from_billed_usage(model, task, cost):
    provider = _priced(_provider(_lifecycle_handler(_task(model=model, **task))))
    result = provider.invoke(_step(model=model))
    assert result.status == StepStatus.SUCCEEDED
    assert result.cost_usd == pytest.approx(cost)


def test_registered_pricing_covers_upscales():
    task = _upscale_task("4K", seconds=7)
    result = _priced(_provider(_lifecycle_handler(task))).invoke(_upscale_step())
    assert result.cost_usd == pytest.approx(0.476)


def test_registered_pricing_keeps_the_family_param_contract():
    provider = _priced(_provider(lambda request: pytest.fail("request must not be sent")))
    with pytest.raises(ProviderError):
        provider.submit(_step(params={"duration": 99}))
    assert float(provider.estimate_cost("MiniMax-H3", {"duration": 10, "resolution": "2K"})) == 1.1


# --- error mapping ----------------------------------------------------------


def _status_error(status: int, body: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _CREATE_URL)
    response = httpx.Response(status, json=body or {}, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("slow"), ProviderErrorCode.TIMEOUT),
        (httpx.ConnectError("refused"), ProviderErrorCode.SERVER_ERROR),
        (httpx.RemoteProtocolError("closed"), ProviderErrorCode.SERVER_ERROR),
        (
            _status_error(400, _error_body("bad_request_error", "unknown model (2013)", 400)),
            ProviderErrorCode.INVALID_INPUT,
        ),
        (
            _status_error(401, _error_body("authorized_error", "unknown API key (1004)", 401)),
            ProviderErrorCode.AUTH_FAILURE,
        ),
        (
            _status_error(402, _error_body("insufficient_balance_error", "balance (1008)", 402)),
            ProviderErrorCode.AUTH_FAILURE,
        ),
        (
            _status_error(403, _error_body("authorized_error", "suspended (1004)", 403)),
            ProviderErrorCode.AUTH_FAILURE,
        ),
        (
            _status_error(404, _error_body("not_found_error", "unknown task (2013)", 404)),
            ProviderErrorCode.INVALID_INPUT,
        ),
        (
            _status_error(408, _error_body("bad_request_error", "body not received (2013)", 408)),
            ProviderErrorCode.TIMEOUT,
        ),
        (
            _status_error(413, _error_body("bad_request_error", "body over 64 MB (2013)", 413)),
            ProviderErrorCode.INVALID_INPUT,
        ),
        (
            _status_error(
                422,
                _error_body(
                    "unprocessable_entity_error",
                    "video description contains sensitive content (1026)",
                    422,
                ),
            ),
            ProviderErrorCode.CONTENT_POLICY,
        ),
        (
            _status_error(429, _error_body("rate_limit_error", "30 in flight (1045)", 429)),
            ProviderErrorCode.RATE_LIMIT,
        ),
        (
            _status_error(500, _error_body("server_error", "our error (1000)", 500)),
            ProviderErrorCode.SERVER_ERROR,
        ),
        (
            _status_error(503, _error_body("server_error", "screening unavailable (1000)", 503)),
            ProviderErrorCode.SERVER_ERROR,
        ),
        # Bodies without the documented error object fall back to the status.
        (_status_error(401), ProviderErrorCode.AUTH_FAILURE),
        (_status_error(422), ProviderErrorCode.CONTENT_POLICY),
        (_status_error(429), ProviderErrorCode.RATE_LIMIT),
        (_status_error(400), ProviderErrorCode.INVALID_INPUT),
        (_status_error(502), ProviderErrorCode.SERVER_ERROR),
    ],
)
def test_map_anex_error(exc, code):
    assert map_anex_error(exc) == code


@pytest.mark.parametrize(
    ("task_code", "code"),
    [
        ("content_policy_violation", ProviderErrorCode.CONTENT_POLICY),
        ("media_too_large", ProviderErrorCode.INVALID_INPUT),
        ("media_unsupported_format", ProviderErrorCode.INVALID_INPUT),
        ("something_else", ProviderErrorCode.MODEL_ERROR),
        (None, ProviderErrorCode.MODEL_ERROR),
    ],
)
def test_map_anex_task_error(task_code, code):
    assert map_anex_task_error(task_code) == code


# --- registry, capabilities, lifecycle helpers -------------------------------


def test_registry_lists_the_documented_models():
    provider = _provider(_lifecycle_handler())
    assert provider.get_capabilities().models == [
        "FastH3-8-Step-V2",
        "MiniMax-H3",
        "MiniMax-H3-Ref2VA",
        "MiniMax-H3-Ref2VA-SMD-4step",
    ]


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("MiniMax-H3", "anex-minimax-h3"),
        ("MiniMax-H3-Ref2VA", "anex-minimax-h3-ref2va"),
        ("MiniMax-H3-Ref2VA-SMD-4step", "anex-minimax-h3-ref2va"),
        ("FastH3-8-Step-V2", "anex-fasth3"),
    ],
)
def test_models_resolve_to_their_family(model, family):
    registry = AnexProvider.create_registry()
    assert registry.match_family(model).family.name == family
    assert registry.get(model).modality == Modality.VIDEO
    # Pricing is user-registered; none ships.
    assert registry.get(model).pricing is None


def test_capabilities_declare_video_and_chain_inputs():
    caps = _provider(_lifecycle_handler()).get_capabilities()
    assert caps.supported_modalities == [Modality.VIDEO]
    assert caps.accepts_chain_input is True
    assert caps.max_duration == 15
    assert caps.resolutions == ["2K", "4K", "768P"]
    assert caps.output_formats == ["video/mp4"]


def test_normalize_params_maps_standard_names_and_keeps_native_ones():
    provider = _provider(_lifecycle_handler())
    assert provider.normalize_params({"aspect_ratio": "9:16", "resolution": "2k"}) == {
        "ratio": "9:16",
        "resolution": "2K",
    }
    # A native ``ratio`` wins over the standard name.
    assert provider.normalize_params({"aspect_ratio": "9:16", "ratio": "1:1"}) == {"ratio": "1:1"}


def test_entry_point_resolves_to_provider():
    (ep,) = [ep for ep in entry_points(group="genblaze.providers") if ep.name == "anex"]
    assert ep.load() is AnexProvider


def test_close_only_closes_an_internal_client():
    owned = AnexProvider(api_key=_KEY, catalog_url=None)
    client = owned._client
    with owned:
        pass
    assert client.is_closed
    owned.close()  # idempotent

    injected = httpx.Client()
    AnexProvider(api_key=_KEY, http_client=injected).close()
    assert not injected.is_closed
    injected.close()


def test_async_invoke_submits_once_and_succeeds():
    import asyncio

    seen: list[httpx.Request] = []
    result = asyncio.run(_provider(_lifecycle_handler(seen=seen)).ainvoke(_step()))
    assert result.status == StepStatus.SUCCEEDED
    assert [r.method for r in seen] == ["POST", "GET"]


def test_timed_out_step_keeps_the_task_id_and_never_resubmits_or_cancels():
    seen: list[httpx.Request] = []
    # A queued task reports an empty ``usage`` object, as the live API does.
    provider = _provider(_lifecycle_handler(_task("queued", usage={}), seen=seen))
    result = provider.invoke(_step(), {"timeout": 0.05})
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    # The task keeps running and is billed; its id is what resume() needs.
    assert result.metadata["upstream_id"] == _TASK_ID
    assert [r.method for r in seen].count("POST") == 1
    assert "DELETE" not in [r.method for r in seen]


# --- catalog discovery ------------------------------------------------------


def test_catalog_is_read_without_the_key_and_off_the_api_client():
    api_seen: list[httpx.Request] = []
    catalog_seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(seen=api_seen), catalog_seen=catalog_seen)
    assert provider.invoke(_step()).status == StepStatus.SUCCEEDED

    (request,) = catalog_seen
    assert request.method == "GET"
    assert str(request.url) == DEFAULT_CATALOG_URL == "https://anex.sh/api/estimate/catalog"
    assert "authorization" not in request.headers
    assert _KEY not in str(request.headers)
    # The API client never talks to the catalog host.
    assert {r.url.host for r in api_seen} == {"tasks.anex.sh"}


def test_catalog_url_does_not_follow_the_api_region():
    catalog_seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(), base_url=EU_BASE_URL, catalog_seen=catalog_seen)
    assert provider.discover_models().source_url == DEFAULT_CATALOG_URL
    assert [str(r.url) for r in catalog_seen] == [DEFAULT_CATALOG_URL]


def test_catalog_url_can_be_overridden_or_turned_off():
    catalog_seen: list[httpx.Request] = []
    provider = _provider(
        _lifecycle_handler(),
        catalog_url="https://mirror.example.com/catalog.json",
        catalog_seen=catalog_seen,
    )
    assert provider.discover_models().status is DiscoveryStatus.OK
    assert [str(r.url) for r in catalog_seen] == ["https://mirror.example.com/catalog.json"]

    off: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(), catalog_url=None, catalog_seen=off)
    assert provider.invoke(_step()).status == StepStatus.SUCCEEDED
    assert provider.validate_model("MiniMax-H3").outcome is ValidationOutcome.OK_PROVISIONAL
    assert off == []


@pytest.mark.parametrize(
    "catalog_url", ["http://anex.sh/api/estimate/catalog", "https://user:pw@anex.sh/c", "anex.sh"]
)
def test_rejects_unsafe_catalog_url(catalog_url):
    with pytest.raises(ValueError, match="catalog_url"):
        AnexProvider(api_key=_KEY, catalog_url=catalog_url)


def test_discovery_lists_the_request_names_of_video_models_only():
    result = _provider(_lifecycle_handler()).discover_models()
    assert result.status is DiscoveryStatus.OK
    assert result.slugs == {
        "FastH3-8-Step-V2",
        "MiniMax-H3",
        "MiniMax-H3-Ref2VA",
        "MiniMax-H3-Ref2VA-SMD-4step",
    }


def test_listed_models_validate_as_authoritative():
    provider = _provider(_lifecycle_handler())
    result = provider.validate_model("MiniMax-H3-Ref2VA")
    assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
    assert result.source is ValidationSource.DISCOVERY


def test_a_new_model_in_the_catalog_works_without_a_connector_change():
    new = _video_entry(
        "nova-2", ["Nova-2", "Nova-2-Turbo"], ["text", "reference"], ["768P", "1080P"], seed=True
    )
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(seen=seen), catalog=_catalog(new))

    result = provider.validate_model("Nova-2-Turbo")
    assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
    assert "Nova-2" in provider.get_capabilities().models
    assert "1080P" in provider.get_capabilities().resolutions

    # Its resolutions and input kinds come from the catalog.
    step = _step(
        model="Nova-2",
        params={"duration": 5, "resolution": "1080p"},
        inputs=[Asset(url="https://cdn.example.com/a.png", media_type="image/png")],
    )
    assert provider.submit(step) == _TASK_ID
    body = json.loads(seen[0].content)
    assert body["model"] == "Nova-2" and body["resolution"] == "1080P"
    assert [item["role"] for item in body["content"][1:]] == ["reference_image"]
    with pytest.raises(ProviderError, match="768P, 1080P"):
        provider.submit(_step(model="Nova-2", params={"duration": 5, "resolution": "4K"}))


def test_a_new_model_runs_through_a_pipeline():
    from genblaze_core import Pipeline

    new = _video_entry("nova-2", ["Nova-2"], ["text"], ["768P"], seed=True)
    provider = _provider(_lifecycle_handler(_task(model="Nova-2")), catalog=_catalog(new))
    result = (
        Pipeline("new-model")
        .step(provider, model="Nova-2", prompt="a fox", modality=Modality.VIDEO, duration=4)
        .run(progress=False, raise_on_failure=False)
    )
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


def test_a_model_removed_from_the_catalog_is_refused_at_preflight():
    from genblaze_core import Pipeline

    api_seen: list[httpx.Request] = []
    provider = _provider(
        _lifecycle_handler(seen=api_seen), catalog=_catalog(drop=("fasth3-8-step-v2",))
    )
    result = provider.validate_model("FastH3-8-Step-V2")
    assert result.outcome is ValidationOutcome.NOT_FOUND
    assert result.source is ValidationSource.DISCOVERY
    assert "FastH3-8-Step-V2" not in provider.get_capabilities().models

    pipeline = Pipeline("retired").step(
        provider, model="FastH3-8-Step-V2", prompt="a fox", modality=Modality.VIDEO, duration=4
    )
    with pytest.raises(ProviderError, match="FastH3-8-Step-V2") as info:
        pipeline.run(progress=False, raise_on_failure=False)
    assert info.value.error_code == ProviderErrorCode.MODEL_ERROR
    # Refused before any task was created.
    assert api_seen == []


def test_a_user_registered_model_is_usable_though_the_catalog_omits_it():
    provider = _provider(_lifecycle_handler())
    assert provider.validate_model("Private-Preview").outcome is ValidationOutcome.NOT_FOUND
    provider.models.register(ModelSpec(model_id="Private-Preview", modality=Modality.VIDEO))
    assert provider.validate_model("Private-Preview").source is ValidationSource.USER
    assert provider.submit(_step(model="Private-Preview")) == _TASK_ID


@pytest.mark.parametrize(
    "answer",
    [
        _UNREADABLE,
        httpx.ConnectError("no route"),
        httpx.Response(200, text="<html>not json</html>"),
        {"models": "nope"},
        # A listing without video models is treated as unreadable, not as
        # "every model was retired".
        {"models": [{"id": "qwen3.8-27b", "task": "generate"}]},
        {"models": [_video_entry("bad", ["../x"], ["text"], ["768P"], seed=True)]},
    ],
    ids=["503", "network", "not-json", "wrong-shape", "no-video", "bad-names"],
)
def test_unreadable_catalog_falls_back_to_the_families(answer):
    seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(seen=seen), catalog=answer)

    assert provider.discover_models().status is DiscoveryStatus.FAILED
    # Family-matched names are provisional, nothing is reported as missing.
    assert provider.validate_model("MiniMax-H3").outcome is ValidationOutcome.OK_PROVISIONAL
    unknown = provider.validate_model("Some-Future-Model")
    assert unknown.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE
    assert provider.get_capabilities().models == [
        "FastH3-8-Step-V2",
        "MiniMax-H3",
        "MiniMax-H3-Ref2VA",
        "MiniMax-H3-Ref2VA-SMD-4step",
    ]
    assert provider.get_capabilities().resolutions == ["2K", "4K", "768P"]

    # Requests are shaped by the built-in families.
    assert provider.invoke(_step(params={"duration": 6, "resolution": "2k"})).status == (
        StepStatus.SUCCEEDED
    )
    assert json.loads(seen[0].content)["resolution"] == "2K"
    with pytest.raises(ProviderError, match="resolution"):
        provider.submit(
            _step(model="FastH3-8-Step-V2", params={"duration": 5, "resolution": "2K"})
        )


def test_catalog_is_read_once_per_cache_window(monkeypatch):
    catalog_seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(), catalog_seen=catalog_seen)
    for _ in range(3):
        assert provider.invoke(_step()).status == StepStatus.SUCCEEDED
        provider.validate_model("MiniMax-H3")
        provider.get_capabilities()
    assert len(catalog_seen) == 1


def test_forced_refresh_never_reads_more_often_than_every_five_minutes(monkeypatch):
    assert provider_module.CATALOG_MIN_INTERVAL_SECONDS == 300
    catalog_seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(), catalog_seen=catalog_seen)
    provider.discover_models()
    for _ in range(3):
        provider.validate_model("MiniMax-H3", refresh=True)
        provider.discover_models(max_age_seconds=0)
        provider.catalog_rates()
    assert len(catalog_seen) == 1

    clock = provider_module.time.monotonic()
    monkeypatch.setattr(provider_module.time, "monotonic", lambda: clock + 301)
    assert provider.discover_models(max_age_seconds=0).status is DiscoveryStatus.OK
    assert len(catalog_seen) == 2


def test_failed_reads_are_not_repeated_within_five_minutes():
    catalog_seen: list[httpx.Request] = []
    provider = _provider(_lifecycle_handler(), catalog=_UNREADABLE, catalog_seen=catalog_seen)
    for _ in range(3):
        assert provider.invoke(_step()).status == StepStatus.SUCCEEDED
    assert len(catalog_seen) == 1


def test_a_failed_refresh_keeps_the_last_catalog(monkeypatch):
    answers = [_catalog(_video_entry("nova-2", ["Nova-2"], ["text"], ["768P"], seed=True))]

    def handler(request):
        return httpx.Response(200, json=answers.pop(0)) if answers else _UNREADABLE

    provider = _provider(
        _lifecycle_handler(),
        catalog_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.discover_models().status is DiscoveryStatus.OK
    clock = provider_module.time.monotonic()
    monkeypatch.setattr(provider_module.time, "monotonic", lambda: clock + 301)
    stale = provider.discover_models(max_age_seconds=0)
    assert stale.status is DiscoveryStatus.STALE and "Nova-2" in stale.slugs
    # The last good profile still shapes the request.
    with pytest.raises(ProviderError, match="Nova-2 renders 768P"):
        provider.submit(_step(model="Nova-2", params={"duration": 5, "resolution": "2K"}))


def test_seed_on_a_request_that_ignores_it_is_logged(caplog):
    provider = _provider(_lifecycle_handler())
    with caplog.at_level("WARNING", logger="genblaze.anex"):
        provider.submit(_step(seed=7))
    assert "does not honour a seed on a text request" in caplog.text

    caplog.clear()
    image = [Asset(url="https://cdn.example.com/a.png", media_type="image/png")]
    with caplog.at_level("WARNING", logger="genblaze.anex"):
        provider.submit(_step(model="MiniMax-H3-Ref2VA", seed=7, inputs=image))
        provider.submit(_step(model="FastH3-8-Step-V2", seed=7))
    assert caplog.text == ""


def test_parser_reads_the_names_kinds_resolutions_and_seed_support():
    catalog = parse_catalog(_catalog())
    h3 = catalog.models["MiniMax-H3"]
    # Two entries list the name MiniMax-H3: the profile is their union.
    assert h3.kinds == {"text", "frames", "reference"}
    assert h3.resolutions == ("768P", "2K", "4K")
    assert h3.seed_kinds == {"reference"}
    fast = catalog.models["FastH3-8-Step-V2"]
    assert (fast.kinds, fast.resolutions, fast.seed_kinds) == ({"text"}, ("768P",), {"text"})


def test_parser_skips_malformed_entries_and_unknown_values():
    catalog = parse_catalog(
        _catalog(
            "not an entry",
            {"id": "no-video-block", "task": "generate_video"},
            _video_entry("odd-kind", ["Odd-1"], ["text", "hologram"], ["768P"], seed=True),
            _video_entry("no-kinds", ["Odd-2"], ["hologram"], ["768P"], seed=True),
            {"id": "future-task", "task": "generate_music", "video": {"names": ["Odd-3"]}},
        )
    )
    assert catalog.models["Odd-1"].kinds == {"text"}
    assert "Odd-2" not in catalog.models and "Odd-3" not in catalog.models
    assert "MiniMax-H3" in catalog.models


# --- compliance harness -----------------------------------------------------


class TestAnexCompliance(ProviderComplianceTests):
    """Runs with the documented pricing recipe registered, so cost is checked."""

    def make_provider(self):
        return _priced(_provider(_lifecycle_handler()))

    def make_step(self):
        return _step()

    def constructor_kwargs_for_probe_cache_test(self):
        return {"api_key": _KEY}


class TestAnexUpscaleCompliance(TestAnexCompliance):
    """Re-run the harness on an upscale step, which uses the other endpoint."""

    def make_provider(self):
        task = _upscale_task("2K")
        return _priced(_provider(_lifecycle_handler(task)))

    def make_step(self):
        return _upscale_step(params={"resolution": "2K", "source_task_id": _TASK_ID})
