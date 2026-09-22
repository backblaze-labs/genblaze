"""fal.ai image, video, and audio generation provider.

fal serves its whole model catalog behind one asynchronous queue API
(https://fal.ai/docs/documentation/model-apis/inference/queue):

- submit: ``POST https://queue.fal.run/{model_id}`` with
  ``Authorization: Key $FAL_KEY``; the body is the model's input JSON. The
  response carries ``request_id``, ``status_url`` and ``response_url``.
- poll: ``GET {request}/status`` → ``{"status": "IN_QUEUE" | "IN_PROGRESS" |
  "COMPLETED", ...}``; a failed request is ``COMPLETED`` with ``error`` /
  ``error_type``.
- fetch: ``GET {request}`` → the model-specific output JSON (``images``,
  ``video``, ``audio_file``, ...), or an error body for a failed request.

``{request}`` is ``https://queue.fal.run/{owner}/{app}/requests/{request_id}``,
the shape fal returns as ``response_url``. The prediction id is that path
relative to the queue host, so it is self-describing: a checkpointed id can be
polled from any process via ``resume()``, and both URLs are always rebuilt on
the configured queue host, which is the only host that ever receives the key.

The connector talks to that HTTP API with httpx instead of depending on the
``fal-client`` SDK. Submission is single-attempt: every submit failure is
wrapped in ``ProviderError``, so core's phase retry never repeats the POST,
because an ambiguous failure could still be a billable generation. Even
pre-response connect failures are not retried, which keeps the rule simple. Status and
result GETs use the provider's ``RetryPolicy`` (bounded backoff, honoring
``Retry-After`` and the step deadline). Step-level ``config["max_retries"]`` is
a caller opt-in that re-runs a failed submit; leave it at 0 for strict
at-most-once submission.
"""

from __future__ import annotations

import mimetypes
import os
import re
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
from genblaze_core.providers.base import classify_api_error
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import describe_fal_error, map_fal_error, map_fal_error_type

_DEFAULT_BASE_URL = "https://queue.fal.run"

# One URL path segment. Starting with an alphanumeric rules out ``..`` and
# empty segments; ``?``, ``#``, ``%`` and ``@`` are excluded so an id can never
# smuggle query params such as ``fal_webhook`` (which would ship the output to
# a third-party URL) or alter the target host.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# fal endpoint ids: ``owner/app[/sub/path]``.
_MODEL_ID_RE = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})+")
# Prediction ids: ``[namespace/]owner/app/requests/{request_id}``.
_QUEUE_PATH_RE = re.compile(rf"(?:{_SEGMENT}/){{2,3}}requests/{_SEGMENT}")
# fal-client's namespaced app ids (``workflows/owner/app``), which keep one
# extra leading segment in the queue path.
_APP_NAMESPACES = frozenset({"workflows", "comfy"})

# Documented queue states; only COMPLETED is terminal.
_COMPLETED = "COMPLETED"
# Codes a terminal request failure keeps; anything else becomes MODEL_ERROR.
_TERMINAL_ERROR_CODES = frozenset(
    {
        ProviderErrorCode.CONTENT_POLICY,
        ProviderErrorCode.INVALID_INPUT,
        ProviderErrorCode.MODEL_ERROR,
    }
)

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


def _origin(parsed: Any) -> str | None:
    """Normalized ``https://host[:port]`` origin of a parsed URL, or None if malformed."""
    try:
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or parsed.username is not None:
        return None
    return f"https://{parsed.hostname}" + (f":{port}" if port not in (None, 443) else "")


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


def _is_url_param(key: Any) -> bool:
    name = str(key).lower()
    return name in _URL_PARAM_NAMES or name.endswith(_URL_PARAM_SUFFIXES)


def _validate_url_params(value: Any, *, url_slot: bool = False) -> None:
    """Validate every string under a URL-bearing key, at any nesting depth.

    Nested inputs (``loras: [{"path": ...}]``, ``reference_images: [{"image_url":
    ...}]``) are walked too; a string counts as a URL input when its nearest
    enclosing key names a URL slot.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_url_params(item, url_slot=_is_url_param(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_url_params(item, url_slot=url_slot)
    elif url_slot and isinstance(value, str) and value:
        _validate_input_url(value)


def _truthy_flag(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1"))


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _media_type(file: dict[str, Any], url: str, key_kind: str | None, fallback: str | None) -> str:
    """Pick an asset MIME type: fal content_type → output key/extension → modality."""
    content_type = file.get("content_type")
    if isinstance(content_type, str) and "/" in content_type:
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            return content_type
    guessed = mimetypes.guess_type(urlparse(url).path)[0]
    # A known output key constrains the kind; otherwise the extension is the
    # better signal than the step's modality (e.g. a PNG thumbnail on a video).
    if guessed and (key_kind is None or guessed.startswith(f"{key_kind}/")):
        return guessed
    kind = key_kind or fallback
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
        http_timeout: Per-request HTTP timeout in seconds.
        http_client: Optional injected client, primarily for tests. The caller
            owns its lifecycle; ``close()`` only closes an internal client.
        models: Optional custom model registry.
        retry_policy: Optional core retry policy for status/result GETs. Submit
            is never retried by it.
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
        http_timeout: float = 60.0,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path.strip("/")
            or any(c in base_url for c in "?#;")
            or parsed.username is not None
            or parsed.password is not None
            or port == -1
            or port == 0
        ):
            raise ValueError(
                f"base_url must be a bare https:// origin (no path, query, fragment or "
                f"credentials), got {base_url!r}"
            )
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self._api_key = api_key or os.getenv("FAL_KEY")
        # Normalized origin: lowercase host, default port elided, so it compares
        # equal to the origins fal puts in response_url.
        port_suffix = f":{port}" if port not in (None, 443) else ""
        self._base_url = f"https://{parsed.hostname}{port_suffix}"
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=http_timeout)

    def close(self) -> None:
        """Release the internal HTTP client's pool; no-op for an injected client."""
        if self._owns_client:
            self._client.close()

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

    def _queue_path(self, data: dict[str, Any], model: str, request_id: str) -> str:
        """Derive the self-describing prediction id for a submitted request.

        Uses fal's ``response_url`` when it is a well-formed request path on the
        configured queue host; otherwise falls back to fal-client's documented
        derivation from the endpoint id (``[namespace/]owner/app``). Either way
        the returned URL is never followed as-is, so a hostile or malformed
        URL in the response cannot redirect the key.
        """
        response_url = data.get("response_url")
        if isinstance(response_url, str):
            parsed = urlparse(response_url)
            path = parsed.path.strip("/")
            if (
                parsed.scheme == "https"
                and _origin(parsed) == self._base_url
                and not (parsed.query or parsed.fragment)
                and _QUEUE_PATH_RE.fullmatch(path)
                and path.endswith(f"/requests/{request_id}")
            ):
                return path
        parts = model.split("/")
        app_len = 3 if parts[0] in _APP_NAMESPACES and len(parts) >= 3 else 2
        return f"{'/'.join(parts[:app_len])}/requests/{request_id}"

    def _request_url(self, prediction_id: Any) -> str:
        """Rebuild the request URL on the trusted queue host from a prediction id."""
        path = str(prediction_id)
        if not _QUEUE_PATH_RE.fullmatch(path):
            raise ProviderError(
                f"Invalid fal prediction id {path!r}; expected "
                "'owner/app/requests/<request_id>' as returned by submit()",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        return f"{self._base_url}/{path}"

    def _get(self, url: str) -> dict[str, Any]:
        return _json_object(self._client.get(url, headers=self._headers()))

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
        """Enqueue exactly one fal request; returns the queue-path prediction id."""
        if not _MODEL_ID_RE.fullmatch(step.model):
            raise ProviderError(
                f"Invalid fal model id {step.model!r}; expected an endpoint id such as "
                "'fal-ai/flux/schnell'",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        headers = self._headers()
        try:
            payload = self.prepare_payload(step)
            if _truthy_flag(payload.get("sync_mode")):
                raise ProviderError(
                    "sync_mode returns inline data URIs instead of hosted media; remove it "
                    "so fal outputs can be recorded as asset URLs",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            _validate_url_params(payload)
            data = _json_object(
                self._client.post(f"{self._base_url}/{step.model}", headers=headers, json=payload)
            )
        except ProviderError:
            raise
        except Exception as exc:
            # Wrapping every failure (including pre-response ones) is what keeps
            # core's submit phase from ever re-sending the POST.
            raise self._wrap("submit", exc) from exc
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(_SEGMENT, request_id):
            raise ProviderError(
                f"fal submit response did not include a usable request_id: {request_id!r}",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        return self._queue_path(data, step.model, request_id)

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        url = f"{self._request_url(prediction_id)}/status"
        try:
            data = self._get(url)
        except Exception as exc:
            raise self._wrap("poll", exc) from exc
        if str(data.get("status", "")).upper() == _COMPLETED:
            self._cache_poll_result(prediction_id, data)
            return True
        return False

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        url = self._request_url(prediction_id)
        request_id = str(prediction_id).rsplit("/", 1)[-1]
        status = self._get_cached_poll_result(prediction_id)
        if status is None:
            # Cold cache (fetch-phase retry, concurrent resume, or a direct
            # fetch): re-read status so a terminal error is still classified
            # from it rather than from a retried read of the stored failure.
            try:
                status = self._get(f"{url}/status")
            except Exception as exc:
                raise self._wrap("poll", exc) from exc
        status = status if isinstance(status, dict) else {}

        # A failed request reports its error on the status body; classify it
        # there instead of re-reading (and retrying) a stored error response.
        error = status.get("error")
        if error:
            error_type = status.get("error_type")
            error_type = error_type if isinstance(error_type, str) else None
            code = map_fal_error_type(error_type) or classify_api_error(str(error))
            # The request is terminal, and fal already re-queues runner failures
            # server-side, so a transient-looking code (timeout / server error)
            # would only make core re-read the same stored failure.
            if code not in _TERMINAL_ERROR_CODES:
                code = ProviderErrorCode.MODEL_ERROR
            label = f"{error_type}: " if error_type else ""
            raise ProviderError(
                f"fal request {request_id} failed: {label}{str(error)[:500]}", error_code=code
            )

        try:
            result = self._get(url)
        except Exception as exc:
            raise self._wrap("output fetch", exc) from exc

        fallback_kind = _MODALITY_KINDS.get(self._models.get(step.model).modality or step.modality)
        assets: list[Asset] = []
        for key, file in _output_files(result):
            url = file["url"]
            try:
                validate_asset_url(url)
            except ProviderError as exc:
                raise ProviderError(
                    f"fal request {request_id} returned an unusable output URL "
                    f"(only hosted https:// media is recorded): {url[:80]!r}",
                    error_code=ProviderErrorCode.MODEL_ERROR,
                ) from exc
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
                f"fal request {request_id} completed without media outputs",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )

        payload: dict[str, Any] = {"request_id": request_id}
        metrics = status.get("metrics")
        inference_time = metrics.get("inference_time") if isinstance(metrics, dict) else None
        if isinstance(inference_time, (int, float)) and not isinstance(inference_time, bool):
            payload["inference_time"] = inference_time
        if _int_or_none(result.get("seed")) is not None:
            payload["seed"] = result["seed"]
        step.assets.extend(assets)
        step.provider_payload = {"fal": payload}
        return step
