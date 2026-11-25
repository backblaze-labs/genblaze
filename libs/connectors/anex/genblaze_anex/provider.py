"""anex.sh video generation provider.

anex.sh serves MiniMax H3 video models behind two public APIs
(https://anex.sh/docs/video). This connector uses the MiniMax-compatible task
API with plain httpx:

- submit: ``POST https://tasks.anex.sh/v2/video_generation`` with
  ``Authorization: Bearer $ANEX_API_KEY``; the body names the ``model``, a
  ``content`` list (one text item plus any frame or reference items),
  ``resolution`` and ``duration``. The response is ``{"task_id": "..."}``.
- poll: ``GET /v2/query/video_generation/{task_id}`` answers
  ``{"task": {...}}`` with ``status`` ``queued``, ``running``, ``succeeded``,
  ``failed`` or ``cancelled``.
- fetch: the same read. A succeeded task carries the clip's link in
  ``content.url`` and its billed units in ``usage``.

A step with ``step_type=StepType.UPSCALE`` goes to
``POST /v2/video_regeneration`` instead, which takes a finished clip to 2K or
4K and is polled the same way.

The task API was chosen over the fal-compatible queue API because one
connector then covers every operation (the queue API has no upscale), a single
read returns both the status and the output, and the task reports its billed
units, which a pricing strategy can read.

Submission is single-attempt: every submit failure is wrapped in
``ProviderError``, so core's phase retry never repeats the POST, because an
ambiguous failure could still be a billable task. Task reads use the
provider's ``RetryPolicy`` (bounded backoff, honoring ``Retry-After`` and the
step deadline). Step-level ``config["max_retries"]`` is a caller opt-in that
re-runs a failed submit; leave it at 0 for strict at-most-once submission.

**Catalog**: the models, and what each accepts, are read from anex.sh's
public catalog (``GET https://anex.sh/api/estimate/catalog``, no key; see
https://anex.sh/docs/video-models). That makes discovery
``DiscoverySupport.NATIVE``: a model added on anex.sh is usable, and a
retired one is refused at preflight, without a connector release. The
catalog is read at most every five minutes and never receives the API key.
When it cannot be read, the ``ModelFamily`` rules below describe the models
known when this version was released.

**Pricing**: no prices ship in the registry. ``cost_usd`` stays ``None``
unless a pricing strategy is registered via
``provider.models.register_pricing(...)``; see the recipe in
``docs/reference/pricing-recipes.md``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepType
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    IntSchema,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    validate_asset_url,
    validate_chain_input_url,
)
from genblaze_core.providers.discovery import (
    DEFAULT_TTL_SECONDS,
    DiscoveryResult,
    DiscoveryStatus,
    _DiscoveryCache,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._catalog import (
    DEFAULT_CATALOG_URL,
    KIND_FRAMES,
    KIND_REFERENCE,
    KIND_TEXT,
    Catalog,
    CatalogRates,
    ModelProfile,
    parse_catalog,
)
from ._errors import describe_anex_error, map_anex_error, map_anex_task_error
from ._version import __version__

DEFAULT_BASE_URL = "https://tasks.anex.sh"
# For organizations that keep their data in the EEA. The other region's host
# answers 421 with a message naming the right one.
EU_BASE_URL = "https://tasks-eu.anex.sh"

# The catalog answers with ``Cache-Control: max-age=300``; it is never read
# more often than that, whatever a caller asks of the discovery cache.
CATALOG_MIN_INTERVAL_SECONDS = 300.0
_CATALOG_TIMEOUT_SECONDS = 5.0
_CATALOG_MAX_BYTES = 4 * 2**20

logger = logging.getLogger("genblaze.anex")

_GENERATION_PATH = "/v2/video_generation"
_REGENERATION_PATH = "/v2/video_regeneration"
_QUERY_PATH = "/v2/query/video_generation"

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Task ids are opaque. One URL path segment starting with an alphanumeric
# rules out ``..``, ``/``, ``?`` and ``#``, so a checkpointed id passed to
# ``resume()`` can never change the request path or add query params.
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# An uploaded file is named ``input:<id>`` in a request.
_UPLOAD_REF_RE = re.compile(r"input:[A-Za-z0-9][A-Za-z0-9_-]*")

_UPSCALE_RESOLUTIONS = frozenset({"2K", "4K"})
_MIN_DURATION = 4
_MAX_DURATION = 15

# Output sizes per (ratio, resolution), from https://anex.sh/docs/video#sizes.
# 2K and 4K clips are a 768P render that is then upscaled.
_SIZES: dict[tuple[str, str], tuple[int, int]] = {
    ("21:9", "768P"): (1536, 672),
    ("21:9", "2K"): (2976, 1248),
    ("21:9", "4K"): (4464, 1872),
    ("16:9", "768P"): (1344, 768),
    ("16:9", "2K"): (2560, 1440),
    ("16:9", "4K"): (3840, 2160),
    ("4:3", "768P"): (1024, 768),
    ("4:3", "2K"): (1920, 1440),
    ("4:3", "4K"): (2880, 2160),
    ("1:1", "768P"): (768, 768),
    ("1:1", "2K"): (1440, 1440),
    ("1:1", "4K"): (2160, 2160),
    ("3:4", "768P"): (768, 1024),
    ("3:4", "2K"): (1440, 1920),
    ("3:4", "4K"): (2160, 2880),
    ("9:16", "768P"): (768, 1344),
    ("9:16", "2K"): (1440, 2560),
    ("9:16", "4K"): (2160, 3840),
}
_FRAME_RATE = 24.0

# Media inputs are named as on anex.sh's fal-compatible API and translated
# into the task API's ``content`` items: (param, item type, role, is a list).
_MEDIA_PARAMS: tuple[tuple[str, str, str, bool], ...] = (
    ("image_url", "image_url", "first_frame", False),
    ("end_image_url", "image_url", "last_frame", False),
    ("reference_image_urls", "image_url", "reference_image", True),
    ("reference_video_urls", "video_url", "reference_video", True),
    ("reference_audio_urls", "audio_url", "reference_audio", True),
)
_REFERENCE_PARAMS = {
    "image": "reference_image_urls",
    "video": "reference_video_urls",
    "audio": "reference_audio_urls",
}


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, error_code=ProviderErrorCode.INVALID_INPUT)


def _media_kind(asset: Asset) -> str:
    return (asset.media_type or "").split("/", 1)[0]


# --- chain-input routing ------------------------------------------------------


def _route_inputs(inputs: Sequence[Asset], kinds: frozenset[str]) -> dict[str, Any]:
    """Route chained inputs by the request kinds the model answers.

    One or two images are the first and last frame on a model that takes
    frames. Anything else (videos, audio files, more images) is a set of
    references on a model that takes references.
    """
    if not inputs:
        return {}
    images_only = all(_media_kind(asset) == "image" for asset in inputs)
    if KIND_FRAMES in kinds and images_only and len(inputs) <= 2:
        out: dict[str, Any] = {"image_url": inputs[0].url}
        if len(inputs) == 2:
            out["end_image_url"] = inputs[1].url
        return out
    if KIND_REFERENCE in kinds:
        out = {}
        for asset in inputs:
            param = _REFERENCE_PARAMS.get(_media_kind(asset))
            if param is None:
                raise _invalid(
                    f"Unsupported reference input type {asset.media_type!r}; "
                    "references are images, videos or audio files"
                )
            out.setdefault(param, []).append(asset.url)
        return out
    if KIND_FRAMES in kinds:
        raise _invalid(
            "This model takes one or two images, as first and last frame; reference "
            "images, audio and videos need a model that takes references"
        )
    raise _invalid("This model is text to video only and takes no input media")


# --- model registry ----------------------------------------------------------


def _whole_seconds(value: Any) -> Any:
    """Accept ``6.0`` for ``6``; anything else is left for the schema to judge."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


_ALL_RESOLUTIONS = ("768P", "2K", "4K")
_PROFILE_EXTRA = "anex_profile"


def _video_spec(profile: ModelProfile) -> ModelSpec:
    """A spec whose ``extras`` hold the profile used when the catalog is unreadable."""
    return ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_coercers={"duration": _whole_seconds},
        param_schemas={"duration": IntSchema(min=_MIN_DURATION, max=_MAX_DURATION)},
        param_defaults={"resolution": "768P"},
        param_required=frozenset({"duration"}),
        extras={_PROFILE_EXTRA: profile},
    )


# The models of https://anex.sh/docs/video-models when this version was
# released. A readable catalog replaces these profiles; they keep the
# connector working without it.
_ANEX_FAMILIES = (
    ModelFamily(
        name="anex-minimax-h3-ref2va",
        pattern=re.compile(r"^MiniMax-H3-Ref2VA(?:-|$)"),
        spec_template=_video_spec(
            ModelProfile(
                kinds=frozenset({KIND_REFERENCE}),
                resolutions=_ALL_RESOLUTIONS,
                seed_kinds=frozenset({KIND_REFERENCE}),
            )
        ),
        description=(
            "MiniMax H3 Ref2VA on anex.sh: reference images, reference audio and "
            "video editing, as the 50-step model or its 4-step distillation."
        ),
        example_slugs=("MiniMax-H3-Ref2VA", "MiniMax-H3-Ref2VA-SMD-4step"),
    ),
    ModelFamily(
        name="anex-minimax-h3",
        pattern=re.compile(r"^MiniMax-H3$"),
        # A MiniMax-H3 request that carries a reference is rendered by the
        # reference model, so the name answers all three kinds.
        spec_template=_video_spec(
            ModelProfile(
                kinds=frozenset({KIND_TEXT, KIND_FRAMES, KIND_REFERENCE}),
                resolutions=_ALL_RESOLUTIONS,
                seed_kinds=frozenset({KIND_REFERENCE}),
            )
        ),
        description=(
            "MiniMax H3 on anex.sh: text to video and first and last frame; with a "
            "reference, MiniMax H3 Ref2VA."
        ),
        example_slugs=("MiniMax-H3",),
    ),
    ModelFamily(
        name="anex-fasth3",
        pattern=re.compile(r"^FastH3-"),
        spec_template=_video_spec(
            ModelProfile(
                kinds=frozenset({KIND_TEXT}),
                resolutions=("768P",),
                seed_kinds=frozenset({KIND_TEXT}),
            )
        ),
        description="FastH3 on anex.sh: 8-step text to video at 768P.",
        example_slugs=("FastH3-8-Step-V2",),
    ),
)
# A model neither the catalog nor a family knows: nothing is assumed about
# it beyond the request shape, and the API judges the rest.
_UNKNOWN_PROFILE = ModelProfile(
    kinds=frozenset({KIND_TEXT, KIND_FRAMES, KIND_REFERENCE}), resolutions=()
)
_ANEX_FALLBACK = _video_spec(_UNKNOWN_PROFILE)


def _request_kind(content: list[dict[str, Any]]) -> str:
    roles = {item.get("role") for item in content}
    if roles & {"reference_image", "reference_video", "reference_audio"}:
        return KIND_REFERENCE
    if roles & {"first_frame", "last_frame"}:
        return KIND_FRAMES
    return KIND_TEXT


# --- request building --------------------------------------------------------


def _validate_source(url: Any, param: str) -> str:
    """Check one media source: an https URL, a data URI or an ``input:<id>`` upload."""
    if not isinstance(url, str) or not url:
        raise _invalid(f"{param} must be a non-empty string, got {type(url).__name__}")
    if url[:5].lower() == "data:" or _UPLOAD_REF_RE.fullmatch(url):
        return url
    validate_chain_input_url(url)
    if urlparse(url).scheme == "file":
        raise _invalid(
            f"anex.sh cannot read local file input {url!r}; upload it (for example to B2) "
            "and pass an https:// URL"
        )
    return url


def _content_items(prompt: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Pop the media params and build the task API's ``content`` list."""
    items: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for param, item_type, role, is_list in _MEDIA_PARAMS:
        value = params.pop(param, None)
        if value is None:
            continue
        if is_list:
            sources = [value] if isinstance(value, str) else value
            if not isinstance(sources, (list, tuple)):
                raise _invalid(f"{param} must be a URL or a list of URLs")
        else:
            sources = [value]
        for source in sources:
            url = _validate_source(source, param)
            items.append({"type": item_type, item_type: {"url": url}, "role": role})
    return items


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(task: dict[str, Any]) -> dict[str, float]:
    """The task's billed units (numbers only), for pricing strategies."""
    usage = task.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        str(key): value
        for key, value in usage.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


class AnexProvider(BaseProvider):
    """Opt-in anex.sh adapter for video generation and upscaling.

    Pass a model name as ``model``, for example ``MiniMax-H3`` (text to
    video, first and last frame), ``MiniMax-H3-Ref2VA`` or
    ``MiniMax-H3-Ref2VA-SMD-4step`` (reference images, reference audio, video
    editing) or ``FastH3-8-Step-V2`` (text to video). The current names, and
    the resolutions and inputs each takes, are read from anex.sh's public
    catalog. ``duration`` (4 to 15 whole seconds) is required; ``resolution``
    defaults to ``768P``.

    Use ``step_type=StepType.UPSCALE`` with ``model="MiniMax-H3"`` and
    ``resolution`` ``2K`` or ``4K`` to upscale a finished clip, named by
    ``source_task_id`` or supplied as a video input.

    Args:
        api_key: anex.sh API key. Defaults to ``ANEX_API_KEY``.
        base_url: Task API origin. Defaults to ``https://tasks.anex.sh``; use
            ``EU_BASE_URL`` (``https://tasks-eu.anex.sh``) for an organization
            that keeps its data in the EEA.
        catalog_url: The public model catalog. It is the same for every
            region, so it does not follow ``base_url``. ``None`` turns the
            catalog off, and the built-in model families are used alone.
        catalog_client: Optional injected client for the catalog, primarily
            for tests. The catalog is never read through ``http_client`` and
            never receives the API key.
        poll_interval: Initial seconds between task polls.
        http_timeout: Per-request HTTP timeout in seconds.
        http_client: Optional injected client, primarily for tests. The caller
            owns its lifecycle; ``close()`` only closes an internal client.
        models: Optional custom model registry.
        retry_policy: Optional core retry policy for task reads. Submit is
            never retried by it.
    """

    name = "anex"
    discovery_support = DiscoverySupport.NATIVE
    """anex.sh's public catalog lists every video model name a request may
    use, without a key and without creating a task, so a name confirmed by
    it is authoritative and one absent from a fresh read is refused at
    preflight. A model the catalog does not list (a private preview, say)
    can still be used by registering a ``ModelSpec`` for it."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(provider_families=_ANEX_FAMILIES, fallback=_ANEX_FALLBACK)

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        catalog_url: str | None = DEFAULT_CATALOG_URL,
        catalog_client: httpx.Client | None = None,
        poll_interval: float = 5.0,
        http_timeout: float = 120.0,
        http_client: httpx.Client | None = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ) -> None:
        if catalog_url is not None:
            catalog = urlparse(catalog_url)
            if catalog.scheme != "https" or not catalog.hostname or catalog.username is not None:
                raise ValueError(
                    f"catalog_url must be an https:// URL without credentials, got {catalog_url!r}"
                )
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
        self._api_key = api_key or os.getenv("ANEX_API_KEY")
        port_suffix = f":{port}" if port not in (None, 443) else ""
        # The only host that ever receives the key.
        self._base_url = f"https://{parsed.hostname}{port_suffix}"
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=http_timeout)
        # The catalog has its own client, so no default header or auth of the
        # API client can reach the catalog host.
        self._catalog_url = catalog_url
        self._owns_catalog_client = catalog_client is None
        self._catalog_client = catalog_client
        self._catalog: Catalog | None = None
        self._catalog_lock = threading.Lock()
        self._catalog_read_at: float | None = None
        self._catalog_last: DiscoveryResult | None = None
        if models is None:
            # The class default registry is shared; the discovery cache hangs
            # off the registry and its fetcher uses this instance's client.
            self._models = self._models.fork()
        self._models._discovery_cache = _DiscoveryCache(
            self._fetch_models,
            default_max_age_seconds=DEFAULT_TTL_SECONDS,
        )

    def close(self) -> None:
        """Release the internal HTTP clients' pools; no-op for injected clients."""
        if self._owns_client:
            self._client.close()
        if self._owns_catalog_client and self._catalog_client is not None:
            self._catalog_client.close()

    # --- catalog discovery (DiscoverySupport.NATIVE) ------------------------

    def _fetch_models(self) -> DiscoveryResult:
        """Fetcher behind ``discover_models``: one read of the public catalog.

        A read within ``CATALOG_MIN_INTERVAL_SECONDS`` of the previous one
        returns that one's result, whether it succeeded or failed.
        """
        if self._catalog_url is None:
            return DiscoveryResult.failed("the catalog is turned off (catalog_url=None)")
        with self._catalog_lock:
            now = time.monotonic()
            if (
                self._catalog_last is not None
                and self._catalog_read_at is not None
                and now - self._catalog_read_at < CATALOG_MIN_INTERVAL_SECONDS
            ):
                return self._catalog_last
            self._catalog_read_at = now
            try:
                if self._catalog_client is None:
                    self._catalog_client = httpx.Client(timeout=_CATALOG_TIMEOUT_SECONDS)
                response = self._catalog_client.get(
                    self._catalog_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": f"genblaze-anex/{__version__}",
                    },
                )
                response.raise_for_status()
                if len(response.content) > _CATALOG_MAX_BYTES:
                    raise ValueError("catalog answer is too large")
                catalog = parse_catalog(response.json())
            except Exception as exc:
                logger.warning("anex.sh catalog could not be read: %s", exc)
                self._catalog_last = DiscoveryResult.failed(
                    f"anex.sh catalog read failed: {exc}", source_url=self._catalog_url
                )
            else:
                self._catalog = catalog
                self._catalog_last = DiscoveryResult.ok(
                    set(catalog.models), source_url=self._catalog_url
                )
            return self._catalog_last

    def discover_models(
        self,
        *,
        max_age_seconds: float | None = ...,  # type: ignore[assignment]
    ) -> DiscoveryResult:
        """Snapshot the model names in anex.sh's catalog. Single-flight, TTL-bounded."""
        cache = self._models._discovery_cache
        assert cache is not None  # wired in __init__
        if max_age_seconds is ...:  # type: ignore[comparison-overlap]
            return cache.get()
        return cache.get(max_age_seconds=max_age_seconds)

    def catalog_rates(self) -> CatalogRates | None:
        """The rates anex.sh's catalog publishes now, or None if it cannot be read.

        For building a pricing strategy without copying prices into code; see
        ``docs/reference/pricing-recipes.md``. May read the catalog (at most
        every five minutes), so call it when registering pricing, not from
        inside a strategy.
        """
        self.discover_models(max_age_seconds=CATALOG_MIN_INTERVAL_SECONDS)
        return self._catalog.rates if self._catalog is not None else None

    def _profile(self, model: str) -> ModelProfile:
        """What ``model`` accepts: from the catalog, else from its family."""
        result = self.discover_models()
        catalog = self._catalog
        if catalog is not None and result.status is not DiscoveryStatus.FAILED:
            profile = catalog.models.get(model)
            if profile is not None:
                return profile
        profile = self._models.get(model).extras.get(_PROFILE_EXTRA)
        return profile if isinstance(profile, ModelProfile) else _UNKNOWN_PROFILE

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image", "video", "audio"],
            accepts_chain_input=True,
            max_duration=float(_MAX_DURATION),
            resolutions=self._known_resolutions(),
            models=self._known_models(),
            output_formats=["video/mp4"],
        )

    def _known_models(self) -> list[str]:
        """Registry names, minus built-in examples a catalog already read no longer lists."""
        names = set(self._models.known())
        if self._catalog is not None:
            examples = {slug for family in _ANEX_FAMILIES for slug in family.example_slugs}
            names -= examples - set(self._catalog.models)
        return sorted(names)

    def _known_resolutions(self) -> list[str]:
        """Resolutions of the catalog already read, else of the built-in families."""
        if self._catalog is not None:
            profiles = list(self._catalog.models.values())
        else:
            profiles = [family.spec_template.extras[_PROFILE_EXTRA] for family in _ANEX_FAMILIES]
        return sorted({r for profile in profiles for r in profile.resolutions})

    def normalize_params(
        self, params: dict[str, Any], modality: Modality | None = None
    ) -> dict[str, Any]:
        """Map standard names to the task API's: ``aspect_ratio`` and ``2k`` / ``768p``."""
        p = dict(params)
        if "aspect_ratio" in p:
            ratio = p.pop("aspect_ratio")
            p.setdefault("ratio", ratio)
        resolution = p.get("resolution")
        if isinstance(resolution, str):
            p["resolution"] = resolution.upper()
        return p

    # --- HTTP helpers -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "anex.sh API key is required. Set ANEX_API_KEY or pass api_key.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": f"genblaze-anex/{__version__}",
        }

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("anex.sh returned a non-object response")
        return payload

    @staticmethod
    def _wrap(phase: str, exc: Exception) -> ProviderError:
        detail = (
            describe_anex_error(exc.response) if isinstance(exc, httpx.HTTPStatusError) else exc
        )
        return ProviderError(
            f"anex.sh {phase} failed: {detail}",
            error_code=map_anex_error(exc),
            retry_after=retry_after_from_response(exc),
        )

    def _get_task(self, prediction_id: Any) -> dict[str, Any]:
        task_id = str(prediction_id)
        if not _TASK_ID_RE.fullmatch(task_id):
            raise _invalid(
                f"Invalid anex.sh prediction id {task_id!r}; expected the task id "
                "returned by submit()"
            )
        try:
            data = self._json_object(
                self._client.get(
                    f"{self._base_url}{_QUERY_PATH}/{task_id}", headers=self._headers()
                )
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise self._wrap("task read", exc) from exc
        task = data.get("task")
        if not isinstance(task, dict):
            raise ProviderError(
                f"anex.sh task read for {task_id} did not include a task object",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        return task

    # --- request bodies -----------------------------------------------------

    def _generation_body(self, step: Step) -> dict[str, Any]:
        if step.negative_prompt:
            raise _invalid("anex.sh video models take no negative prompt")
        if not step.prompt:
            raise _invalid("anex.sh video generation needs a prompt")
        base: dict[str, Any] = dict(step.params)
        if step.seed is not None:
            base.setdefault("seed", step.seed)
        params = self.prepare_payload(step, base_params=base)
        params.pop("prompt", None)
        params.pop("model", None)
        profile = self._profile(step.model)
        resolution = params.get("resolution")
        if profile.resolutions and resolution not in profile.resolutions:
            raise _invalid(
                f"Invalid parameter 'resolution': {step.model} renders "
                f"{', '.join(profile.resolutions)}, got {resolution!r}"
            )
        # User params win over chained inputs.
        for key, value in _route_inputs(step.inputs, profile.kinds).items():
            params.setdefault(key, value)
        content = _content_items(step.prompt, params)
        kind = _request_kind(content)
        if "seed" in params and kind in profile.kinds and kind not in profile.seed_kinds:
            logger.warning(
                "%s does not honour a seed on a %s request; the clip is not reproducible "
                "from seed %r",
                step.model,
                kind,
                params["seed"],
            )
        return {**params, "model": step.model, "content": content}

    def _upscale_body(self, step: Step) -> dict[str, Any]:
        for asset in step.inputs:
            validate_chain_input_url(asset.url)
        params = self.normalize_params(dict(step.params), step.modality)
        params.pop("model", None)
        resolution = params.get("resolution")
        if resolution not in _UPSCALE_RESOLUTIONS:
            raise _invalid(
                f"An upscale needs resolution '2K' or '4K', got {resolution!r}; the "
                "target must be larger than the clip"
            )
        source_task_id = params.pop("source_task_id", None)
        video_url = params.pop("video_url", None)
        if source_task_id is None and video_url is None:
            videos = [a.url for a in step.inputs if _media_kind(a) == "video"]
            if len(videos) > 1:
                raise _invalid(f"An upscale takes one clip, got {len(videos)} video inputs")
            video_url = videos[0] if videos else None
        if (source_task_id is None) == (video_url is None):
            raise _invalid(
                "An upscale needs exactly one source: source_task_id (a finished task) "
                "or one video (video_url or a video input)"
            )
        body: dict[str, Any] = {**params, "model": step.model}
        if source_task_id is not None:
            source_task_id = str(source_task_id)
            if not _TASK_ID_RE.fullmatch(source_task_id):
                raise _invalid(f"Invalid source_task_id {source_task_id!r}")
            body["source_task_id"] = source_task_id
        else:
            url = _validate_source(video_url, "video_url")
            body["content"] = [
                {"type": "video_url", "video_url": {"url": url}, "role": "base_video"}
            ]
        return body

    # --- lifecycle ----------------------------------------------------------

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create exactly one task; returns its id."""
        headers = self._headers()
        if step.step_type == StepType.UPSCALE:
            path, body = _REGENERATION_PATH, self._upscale_body(step)
        else:
            path, body = _GENERATION_PATH, self._generation_body(step)
        try:
            data = self._json_object(
                self._client.post(f"{self._base_url}{path}", headers=headers, json=body)
            )
        except Exception as exc:
            # Wrapping every failure (including pre-response ones) is what keeps
            # core's submit phase from ever re-sending the POST.
            raise self._wrap("submit", exc) from exc
        task_id = data.get("task_id")
        if isinstance(task_id, int) and not isinstance(task_id, bool):
            task_id = str(task_id)
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ProviderError(
                f"anex.sh submit response did not include a usable task_id: {task_id!r}",
                error_code=ProviderErrorCode.SERVER_ERROR,
            )
        return task_id

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        task = self._get_task(prediction_id)
        if str(task.get("status", "")).lower() in _TERMINAL_STATUSES:
            self._cache_poll_result(prediction_id, task)
            return True
        return False

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        task = self._get_cached_poll_result(prediction_id)
        if not isinstance(task, dict):
            # Cold cache: a fetch-phase retry, a concurrent resume or a direct fetch.
            task = self._get_task(prediction_id)
        task_id = str(prediction_id)
        status = str(task.get("status", "")).lower()

        if status == "failed":
            error = task.get("error")
            error = error if isinstance(error, dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else None
            message = str(error.get("message") or code or "no error message")[:500]
            label = f"{code}: " if code and code not in message else ""
            raise ProviderError(
                f"anex.sh task {task_id} failed: {label}{message}",
                error_code=map_anex_task_error(code),
            )
        if status == "cancelled":
            raise ProviderError(
                f"anex.sh task {task_id} was cancelled", error_code=ProviderErrorCode.UNKNOWN
            )
        if status != "succeeded":
            raise ProviderError(
                f"anex.sh task {task_id} has not finished (status {status or 'unknown'!r})",
                error_code=ProviderErrorCode.UNKNOWN,
            )

        content = task.get("content")
        url = content.get("url") if isinstance(content, dict) else None
        if not isinstance(url, str) or not url:
            raise ProviderError(
                f"anex.sh task {task_id} succeeded without a clip link",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )
        try:
            validate_asset_url(url)
        except ProviderError as exc:
            raise ProviderError(
                f"anex.sh task {task_id} returned an unusable clip link "
                "(only hosted https:// media is recorded)",
                error_code=ProviderErrorCode.MODEL_ERROR,
            ) from exc

        resolution = task.get("resolution") if isinstance(task.get("resolution"), str) else None
        ratio = task.get("ratio") if isinstance(task.get("ratio"), str) else None
        task_type = task.get("task_type") if isinstance(task.get("task_type"), str) else None
        generated = task_type != "regeneration"
        # An upscaled clip of your own is cut to the nearest listed ratio, and
        # keeps its sound only if it had any, so sizes and the audio flag are
        # recorded for generated clips alone.
        size = _SIZES.get((ratio or "", resolution or "")) if generated else None
        width, height = size if size else (None, None)
        step.assets.append(
            Asset(
                url=url,
                media_type="video/mp4",
                width=width,
                height=height,
                video=VideoMetadata(
                    frame_rate=_FRAME_RATE,
                    has_audio=True if generated else None,
                    resolution=resolution,
                ),
            )
        )

        payload: dict[str, Any] = {"task_id": task_id, "status": status}
        if task_type:
            payload["task_type"] = task_type
        if resolution:
            payload["resolution"] = resolution
        if ratio:
            payload["ratio"] = ratio
        duration = _int_or_none(task.get("duration"))
        if duration is not None:
            payload["duration"] = duration
        usage = _usage(task)
        if usage:
            payload["usage"] = usage
        step.provider_payload = {"anex": payload}
        self._apply_registry_pricing(step)
        return step
