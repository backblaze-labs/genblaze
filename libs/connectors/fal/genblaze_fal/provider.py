"""fal.ai image, video, and audio generation provider.

fal serves its whole model catalog behind one asynchronous queue API
(https://fal.ai/docs/documentation/model-apis/inference/queue):

- submit: ``POST https://queue.fal.run/{model_id}`` with
  ``Authorization: Key $FAL_KEY``; the body is the model's input JSON. The
  response carries ``request_id``, ``status_url`` and ``response_url``.
- poll: ``GET status_url`` → ``{"status": "IN_QUEUE" | "IN_PROGRESS" |
  "COMPLETED", ...}``.
- fetch: ``GET response_url`` → the model-specific output JSON (``images``,
  ``video``, ``audio_file``, ...), or an error body for a failed request.

The connector talks to that HTTP API with httpx instead of depending on the
``fal-client`` SDK. Submission is deliberately single-attempt: a transport
failure after a POST is ambiguous, and submitting again could create a second
billable generation. Only the idempotent GETs retry, and that is bounded.

The API key is sent only in the ``Authorization`` header, and only to the
configured queue host; queue URLs returned by fal are checked before any
credentialed request follows them.
"""

from __future__ import annotations

import mimetypes
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    route_by_media_type,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import describe_fal_error, map_fal_error

_DEFAULT_BASE_URL = "https://queue.fal.run"

# fal endpoint ids are ``owner/app[/sub/path]``. Every segment must start with
# an alphanumeric, which rules out ``..`` traversal and empty segments; ``?``
# and ``#`` are excluded so a model id can never smuggle query parameters such
# as ``fal_webhook`` (which would ship the output to a third-party URL).
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+$")

# Documented queue states; only COMPLETED is terminal (a failed request is
# COMPLETED with an ``error`` field, and its response_url returns the error).
_COMPLETED = "COMPLETED"

# Transient GET failures worth a bounded retry. Status and result reads are
# idempotent, so retrying them can never duplicate a billable job.
_TRANSIENT_GET_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
_RETRYABLE_GET_STATUSES = frozenset({429, 502, 503})

# Bounds the request-id → queue-URL map when callers abandon jobs (timeouts).
_MAX_TRACKED_REQUESTS = 1024

# fal's convention for file inputs is ``*_url`` / ``*_urls``; the bare media
# names cover models that follow the generic ``image``/``video``/``audio`` shape.
_URL_PARAM_NAMES = frozenset({"image", "video", "audio"})
_URL_PARAM_SUFFIXES = ("_url", "_urls")

# Chain inputs land on fal's conventional input slots.
_FAL_INPUT_MAPPING = route_by_media_type(
    {"image": "image_url", "video": "video_url", "audio": "audio_url"}
)

# Output keys whose name identifies the media kind when a File object carries
# no usable ``content_type``.
_OUTPUT_KEY_KINDS = {
    "images": "image",
    "image": "image",
    "video": "video",
    "videos": "video",
    "video_url": "video",
    "audio": "audio",
    "audio_file": "audio",
    "audio_url": "audio",
}
_DEFAULT_MEDIA_TYPES = {"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}
_MODALITY_KINDS = {Modality.IMAGE: "image", Modality.VIDEO: "video", Modality.AUDIO: "audio"}

# Starter families for well-known endpoints. Any other slug resolves through
# the permissive fallback and is forwarded unchanged.
_FAL_FAMILIES = (
    ModelFamily(
        name="fal-flux",
        pattern=re.compile(r"^fal-ai/flux(?:[-/]|$)"),
        spec_template=ModelSpec(
            model_id="*", modality=Modality.IMAGE, input_mapping=_FAL_INPUT_MAPPING
        ),
        description="Black Forest Labs FLUX image models on fal.",
        example_slugs=("fal-ai/flux/schnell", "fal-ai/flux/dev"),
    ),
    ModelFamily(
        name="fal-wan-video",
        pattern=re.compile(r"^fal-ai/wan/.+-to-video$"),
        spec_template=ModelSpec(
            model_id="*", modality=Modality.VIDEO, input_mapping=_FAL_INPUT_MAPPING
        ),
        description="Wan text/image-to-video models on fal.",
        example_slugs=("fal-ai/wan/v2.2-a14b/text-to-video",),
    ),
    ModelFamily(
        name="fal-stable-audio",
        pattern=re.compile(r"^fal-ai/stable-audio$"),
        spec_template=ModelSpec(
            model_id="*",
            modality=Modality.AUDIO,
            # Standard ``duration`` (seconds) → Stable Audio's native knob.
            param_aliases={"duration": "seconds_total"},
        ),
        description="Stability AI Stable Audio Open on fal.",
        example_slugs=("fal-ai/stable-audio",),
    ),
)
_FAL_FALLBACK = ModelSpec(model_id="*", input_mapping=_FAL_INPUT_MAPPING)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("fal returned a non-object response")
    return payload


def _validate_input_url(url: str) -> None:
    """SSRF-check one URL-bearing model input before it is forwarded."""
    # Inline data URIs are documented fal inputs and reference no host.
    if url[:5].lower() == "data:":
        return
    validate_chain_input_url(url)
    if urlparse(url).scheme == "file":
        raise ProviderError(
            f"fal cannot read local file input {url!r}; upload it (for example to B2) "
            "and pass an https:// URL",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _validate_url_params(payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        if key not in _URL_PARAM_NAMES and not key.endswith(_URL_PARAM_SUFFIXES):
            continue
        for item in value if isinstance(value, (list, tuple)) else (value,):
            if isinstance(item, str) and item:
                _validate_input_url(item)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _media_type(file: dict[str, Any], url: str, key_kind: str | None, fallback: str | None) -> str:
    """Pick an asset MIME type: fal content_type → key/extension → modality."""
    content_type = file.get("content_type")
    if isinstance(content_type, str) and "/" in content_type:
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            return content_type
    kind = key_kind or fallback
    guessed = mimetypes.guess_type(urlparse(url).path)[0]
    if guessed and (kind is None or guessed.startswith(f"{kind}/")):
        return guessed
    return _DEFAULT_MEDIA_TYPES.get(kind or "", "application/octet-stream")


def _output_files(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Collect ``(output_key, File)`` pairs from a fal result's top level."""
    files: list[tuple[str, dict[str, Any]]] = []
    for key, value in result.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                files.append((key, item))
            elif isinstance(item, str) and key in _OUTPUT_KEY_KINDS and key.endswith("_url"):
                # Some audio/speech models return a bare ``audio_url`` string.
                files.append((key, {"url": item}))
    return files


class FalProvider(BaseProvider):
    """Opt-in fal.ai adapter for image, video, and audio generation.

    Pass any fal endpoint id as ``model`` (e.g. ``fal-ai/flux/schnell``);
    model-specific inputs go in ``params`` and are forwarded unchanged after
    URL-bearing inputs are validated.

    Args:
        api_key: fal API key. Defaults to ``FAL_KEY``.
        base_url: Queue origin. Defaults to ``https://queue.fal.run``.
        poll_interval: Initial seconds between status polls.
        poll_get_retries: Bounded retries for transient status/result GETs.
        http_timeout: Per-request HTTP timeout in seconds.
        http_client: Optional injected client, primarily for tests.
        models: Optional custom model registry.
        retry_policy: Optional core retry policy. The default has one attempt so
            the core never repeats a potentially billable POST.
    """

    name = "fal"
    discovery_support = DiscoverySupport.NONE

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(provider_families=_FAL_FAMILIES, fallback=_FAL_FALLBACK)

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        poll_interval: float = 2.0,
        poll_get_retries: int = 2,
        http_timeout: float = 60.0,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ) -> None:
        if poll_get_retries < 0:
            raise ValueError("poll_get_retries must be >= 0")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"base_url must be an absolute https:// URL, got {base_url!r}")
        super().__init__(
            models=models,
            retry_policy=retry_policy or RetryPolicy(max_attempts=1),
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self.poll_get_retries = poll_get_retries
        self._api_key = api_key or os.getenv("FAL_KEY")
        self._base_url = base_url.rstrip("/")
        self._queue_host = parsed.hostname
        self._client = http_client or httpx.Client(timeout=http_timeout)
        # request_id → (status_url, response_url) from the submit response.
        # fal derives these from the app id, not the full endpoint path, so
        # they are taken from fal rather than rebuilt.
        self._requests: dict[str, tuple[str, str]] = {}

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO, Modality.AUDIO],
            supported_inputs=["text", "image", "video", "audio"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["image/jpeg", "image/png", "video/mp4", "audio/mpeg", "audio/wav"],
        )

    # --- HTTP helpers -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "fal API key is required. Set FAL_KEY or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return {"Authorization": f"Key {self._api_key}", "Accept": "application/json"}

    def _trusted_queue_url(self, url: Any, field: str, request_id: str) -> str:
        """Accept a fal-returned URL only if it points back at the queue host."""
        if not isinstance(url, str) or not url:
            raise ProviderError(
                f"fal submit response for request {request_id} is missing {field}",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._queue_host
            or parsed.username is not None
            or parsed.port not in (None, 443)
        ):
            raise ProviderError(
                f"fal returned {field} outside {self._queue_host} for request "
                f"{request_id}; refusing to send credentials to it",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        return url

    def _track(self, request_id: str, urls: tuple[str, str]) -> None:
        if len(self._requests) >= _MAX_TRACKED_REQUESTS:
            self._requests.pop(next(iter(self._requests)), None)
        self._requests[request_id] = urls

    def _urls_for(self, request_id: Any) -> tuple[str, str]:
        urls = self._requests.get(str(request_id))
        if urls is None:
            raise ProviderError(
                f"unknown fal request {request_id!r}: queue URLs are tracked by the "
                "FalProvider instance that submitted it; poll or resume with that instance",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        return urls

    def _get_json(self, url: str) -> dict[str, Any]:
        """GET with bounded retries on transient failures only."""
        attempt = 0
        while True:
            try:
                response = self._client.get(url, headers=self._headers())
            except _TRANSIENT_GET_ERRORS:
                if attempt >= self.poll_get_retries:
                    raise
            else:
                if (
                    response.status_code not in _RETRYABLE_GET_STATUSES
                    or attempt >= self.poll_get_retries
                ):
                    return _json_object(response)
            time.sleep(min(0.25 * (2**attempt), 1.0))
            attempt += 1

    @staticmethod
    def _wrap(phase: str, exc: Exception) -> ProviderError:
        detail = (
            describe_fal_error(exc.response) if isinstance(exc, httpx.HTTPStatusError) else exc
        )
        return ProviderError(
            f"fal {phase} failed: {detail}",
            error_code=map_fal_error(exc),
            retry_after=retry_after_from_response(exc),
        )

    # --- lifecycle ----------------------------------------------------------

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Enqueue exactly one fal request; this method never retries."""
        if not _MODEL_ID_RE.match(step.model):
            raise ProviderError(
                f"Invalid fal model id {step.model!r}; expected an endpoint id such as "
                "'fal-ai/flux/schnell'",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        payload = self.prepare_payload(step)
        if payload.get("sync_mode"):
            raise ProviderError(
                "sync_mode returns inline data URIs instead of hosted media; remove it "
                "so fal outputs can be recorded as asset URLs",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        _validate_url_params(payload)
        headers = self._headers()
        try:
            data = _json_object(
                self._client.post(f"{self._base_url}/{step.model}", headers=headers, json=payload)
            )
        except Exception as exc:
            raise self._wrap("submit", exc) from exc
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ProviderError(
                "fal submit response did not include a request_id",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        status_url = self._trusted_queue_url(data.get("status_url"), "status_url", request_id)
        response_url = self._trusted_queue_url(
            data.get("response_url"), "response_url", request_id
        )
        self._track(request_id, (status_url, response_url))
        return request_id

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        status_url, _ = self._urls_for(prediction_id)
        try:
            data = self._get_json(status_url)
        except Exception as exc:
            raise self._wrap("poll", exc) from exc
        if str(data.get("status", "")).upper() == _COMPLETED:
            self._cache_poll_result(prediction_id, data)
            return True
        return False

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        _, response_url = self._urls_for(prediction_id)
        try:
            # A failed request answers response_url with its error body, which
            # carries the machine-readable type map_fal_error needs.
            result = self._get_json(response_url)
        except Exception as exc:
            raise self._wrap("output fetch", exc) from exc

        fallback_kind = _MODALITY_KINDS.get(self._models.get(step.model).modality or step.modality)
        assets: list[Asset] = []
        for key, file in _output_files(result):
            url = file["url"]
            validate_asset_url(url)
            media_type = _media_type(file, url, _OUTPUT_KEY_KINDS.get(key), fallback_kind)
            size = _int_or_none(file.get("file_size"))
            assets.append(
                Asset(
                    url=url,
                    media_type=media_type,
                    width=_int_or_none(file.get("width")),
                    height=_int_or_none(file.get("height")),
                    size_bytes=size if size is not None and size >= 0 else None,
                    audio=AudioMetadata() if media_type.startswith("audio/") else None,
                )
            )
        if not assets:
            raise ProviderError(
                f"fal request {prediction_id} completed without media outputs",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )
        step.assets.extend(assets)

        payload: dict[str, Any] = {"request_id": str(prediction_id)}
        status = self._get_cached_poll_result(prediction_id) or {}
        inference_time = (status.get("metrics") or {}).get("inference_time")
        if isinstance(inference_time, (int, float)):
            payload["inference_time"] = inference_time
        if _int_or_none(result.get("seed")) is not None:
            payload["seed"] = result["seed"]
        step.provider_payload = {"fal": payload}
        self._requests.pop(str(prediction_id), None)
        return step
