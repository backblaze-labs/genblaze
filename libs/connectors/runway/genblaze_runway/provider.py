"""RunwayProvider — adapter for the Runway Gen video API.

Uses the runwayml Python SDK with async task-based workflow:
  client.image_to_video.create() → poll task → get output URL

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships pattern-keyed
``ModelFamily`` rules rather than a hardcoded slug list. The Runway Gen
family captures any ``gen<N>[a]_turbo`` slug — Gen-3, Gen-3a, Gen-4,
plus future variants (Gen-5, Gen-4a, etc.) inherit the same param shape
without an SDK release.

**DiscoverySupport.NONE**: Runway has no ``GET /v1/models`` endpoint and
the runwayml SDK doesn't expose raw HTTP for the empty-payload probe
pattern. The catalog is small and stable; submit-time errors are the
authoritative liveness signal. Pipeline preflight emits
``OK_PROVISIONAL`` (matched a family) or ``UNKNOWN_PERMISSIVE`` for
unrecognized slugs — neither raises pre-flight.

**Pricing**: Runway was previously hardcoded as ``(model, duration) →
USD`` (Gen-4 Turbo: $0.50 / $1.00 for 5s / 10s; Gen-3a Turbo: $0.25 /
$0.50). As of 0.3.0 the SDK no longer ships pricing — register the
recipe yourself if you want cost tracking. See
``docs/reference/pricing-recipes.md`` for the canonical Runway recipe.

**Two submit endpoints, routed by input presence (#226)**: the pinned SDK
(``runwayml>=0.6,<5``, resolving to 4.7.0 today) exposes both
``image_to_video.create()`` (always requires ``prompt_image``; no
text-only combination) and ``text_to_video.create()`` (no image input at
all — ``model``/``prompt_text``/``ratio`` required). ``submit()`` picks
the endpoint from whether the step has an input image:

- **Image present** → ``image_to_video``. gen3a_turbo's accepted ratio set
  is disjoint from every other image_to_video model's, so its default is
  resolved per-model (see ``_default_ratio_for_model``); a
  ``params``-supplied ``prompt_image`` is SSRF-validated the same as a
  chained one.
- **No image** → gen4_turbo/gen3a_turbo are image-to-video *only* — the
  pinned SDK's ``text_to_video.create`` doesn't accept them as a ``model``
  literal at all — so a text-only request to either raises a clear
  ``ProviderError`` instead of the SDK's generic "Missing required
  arguments" message or a route to an endpoint that would reject the
  model outright. Every other slug (``gen4.5``, ``veo3``, ``veo3.1``,
  ``veo3.1_fast``, and any future/unrecognized slug via the permissive
  fallback) routes to ``text_to_video`` — duration/ratio value ranges
  differ per model there too and aren't strictly validated client-side;
  submit-time errors are authoritative (see ``DiscoverySupport.NONE``
  below).

Docs: https://docs.runwayml.com/
"""

from __future__ import annotations

import re
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, VideoMetadata
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
    route_images,
    validate_asset_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_runway_error

_VALID_DURATIONS = frozenset({5, 10})

# Runway's ``ratio`` is a pixel-dimension string (e.g. "1280:720"), not a
# colon aspect ratio like "16:9" — verified against runwayml SDK 4.7.0's
# ``image_to_video.create`` overloads (the pin is ``runwayml>=0.6,<5``,
# which resolves to 4.x today). This is the union of every ratio literal
# across the models that route through this endpoint (gen4_turbo, gen4.5,
# veo3, veo3.1, veo3.1_fast share one set; gen3a_turbo has its own disjoint
# set — {"768:1280", "1280:768"} — hence the union rather than one shared
# constant). Each model only accepts a subset of these, so a value accepted
# here can still be rejected server-side for the wrong model. That's fine —
# submit-time errors are the authoritative signal (see
# ``DiscoverySupport.NONE`` above); this check only rejects values no model
# accepts.
_VALID_RATIOS = frozenset(
    {
        "1280:720",
        "720:1280",
        "1104:832",
        "832:1104",
        "960:960",
        "1584:672",
        "1080:1920",
        "1920:1080",
        "768:1280",
        "1280:768",
    }
)

# Landscape default when the caller doesn't supply a ratio — valid for every
# current model *except* gen3a_turbo, whose accepted set doesn't include it
# (see ``_default_ratio_for_model`` below). The SDK requires ``ratio`` in
# most (though not all) valid argument combinations.
_DEFAULT_RATIO = "1280:720"

# gen3a_turbo's ratio set — {"768:1280", "1280:768"} — is disjoint from every
# other model's, so it needs its own default. ``param_defaults`` on
# ModelSpec can't express a per-model value (the family template is shared
# across every matched slug), so the default is resolved here, in submit(),
# where ``step.model`` is available, instead.
_GEN3A_TURBO_DEFAULT_RATIO = "1280:768"


def _default_ratio_for_model(model_id: str) -> str:
    """Pick a landscape default ratio valid for the resolved model.

    gen3a_turbo is the one model this connector targets with a ratio set
    disjoint from everyone else's; every other current and expected future
    slug (gen4_turbo, gen4.5, veo3, veo3.1, veo3.1_fast, and future
    ``*_turbo`` variants) accepts ``_DEFAULT_RATIO``.
    """
    if model_id == "gen3a_turbo":
        return _GEN3A_TURBO_DEFAULT_RATIO
    return _DEFAULT_RATIO


# gen4.5 and veo3's image_to_video.create overloads mark `duration` as
# required (no Omit) — unlike gen4_turbo/gen3a_turbo/veo3.1/veo3.1_fast,
# where it's optional. veo3 additionally pins it to exactly 8 (Literal[8]);
# 8 is also within gen4.5's documented 2-10 range, so one constant covers
# both without a per-model split.
_IMAGE_REQUIRED_DURATION_MODELS = frozenset({"gen4.5", "veo3"})
_IMAGE_REQUIRED_DURATION_DEFAULT = 8


def _default_duration_for_image_model(model_id: str) -> int | None:
    """Return a required duration default for image_to_video, or None.

    Every other model on this endpoint (gen4_turbo, gen3a_turbo, veo3.1,
    veo3.1_fast) accepts an *absent* duration — for those, returning None
    preserves the pre-existing behavior of only forwarding duration when
    the caller supplied one.
    """
    if model_id in _IMAGE_REQUIRED_DURATION_MODELS:
        return _IMAGE_REQUIRED_DURATION_DEFAULT
    return None


def _check_ratio(params: dict[str, Any]) -> None:
    """Validate the (post-alias) Runway-native ``ratio`` value."""
    ratio = params.get("ratio")
    if ratio is not None and ratio not in _VALID_RATIOS:
        raise ProviderError(
            f"Invalid ratio={ratio!r}. Must be one of {set(_VALID_RATIOS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


def _check_duration(params: dict[str, Any]) -> None:
    """Validate ``duration`` with Runway-specific error wording."""
    if "duration" not in params:
        return
    try:
        dur = int(params["duration"])
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            f"Invalid duration={params['duration']!r}. Must be one of {set(_VALID_DURATIONS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        ) from exc
    if dur not in _VALID_DURATIONS:
        raise ProviderError(
            f"Invalid duration={dur}. Must be one of {set(_VALID_DURATIONS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    params["duration"] = dur


def _raise_image_required_error(model_id: str) -> None:
    """Raise an actionable error instead of letting the SDK's leak through.

    Called explicitly from ``submit()`` — NOT wired as a blanket
    ``param_constraint`` — because the image requirement is endpoint-specific,
    not model-specific: models with ``extras["image_only"]`` set (see
    ``_RUNWAY_GEN_FAMILY``) only ever go through ``image_to_video`` (see
    ``@required_args`` on ``ImageToVideoResource.create`` in the runwayml
    SDK, which never omits ``prompt_image``), so a text-only request to one
    of those has no valid endpoint to route to. Every other model can take
    the ``text_to_video`` path instead when no image is given (see
    ``_submit_text_to_video``). Without this check, a text-only request to
    an image-only model would surface the SDK's generic "Missing required
    arguments; Expected either (...)" message (#226) instead of telling the
    caller what to do about it. The caller (``submit()``) has already
    confirmed no image was given, so this always raises — there's no
    condition left to check here.
    """
    raise ProviderError(
        f"{model_id} is image-to-video only and requires an input "
        "image (routed to 'prompt_image'). Chain an image-producing "
        "step (e.g. `.step(..., input_from=[N])` or "
        "`external_inputs=[image_asset]`), pass one directly via "
        "params={'prompt_image': '<https-url>'}, or use a text-capable "
        "model (gen4.5, veo3, veo3.1, veo3.1_fast) for text-only prompts.",
        error_code=ProviderErrorCode.INVALID_INPUT,
    )


def _validate_prompt_image(prompt_image: Any) -> None:
    """SSRF-guard ``prompt_image`` regardless of which shape it arrives in.

    The SDK accepts ``prompt_image`` as either a single URL/data-URI string
    or an iterable of dicts with a ``uri`` field (multi-frame first/last-
    frame arrays, e.g. for veo3.1/veo3.1_fast). Images routed from
    ``step.inputs`` already passed ``validate_chain_input_url`` upstream (in
    ``prepare_payload``); this closes the gap for values supplied directly
    via ``step.params``, which aren't validated anywhere else.

    ``data:`` URIs are a local, no-network-fetch image submission — not an
    SSRF vector — and are left alone. Every ``http``/``https`` value (in
    either shape) is validated with ``validate_asset_url`` (https-only,
    matching the SDK's documented "A HTTPS URL" contract for this field).
    """

    def _check_one(uri: Any) -> None:
        if isinstance(uri, str) and not uri.startswith("data:"):
            validate_asset_url(uri)

    if isinstance(prompt_image, str):
        _check_one(prompt_image)
    elif isinstance(prompt_image, (list, tuple)):
        for item in prompt_image:
            _check_one(item.get("uri") if isinstance(item, dict) else item)


# --- text_to_video (no image input) ----------------------------------------

# Text-capable models per the pinned SDK's text_to_video.create @required_args
# — gen4_turbo/gen3a_turbo are NOT accepted `model` literals there at all;
# Runway's *_turbo line is image-to-video only. Ratio sets differ from
# image_to_video's (e.g. gen4.5's text-to-video set is narrower than its
# image-to-video set), hence a separate constant rather than reusing
# _VALID_RATIOS.
_TEXT_VALID_RATIOS = frozenset({"1280:720", "720:1280", "1080:1920", "1920:1080"})

# Valid for every text-capable model (gen4.5's narrower {"1280:720",
# "720:1280"} set included) — same reasoning as _DEFAULT_RATIO, different
# value space.
_TEXT_DEFAULT_RATIO = "1280:720"

# gen4.5 and veo3 effectively require `duration` (no Omit in their
# text_to_video.create overload); veo3.1/veo3.1_fast make it optional. 8 is
# valid for all four (gen4.5: 2-10 range; veo3: must be exactly 8;
# veo3.1/veo3.1_fast: {4, 6, 8}), so it's a safe universal default rather
# than guessing per-model.
_TEXT_DEFAULT_DURATION = 8


def _check_text_ratio(params: dict[str, Any]) -> None:
    """Validate ``ratio`` for the text_to_video path (separate value space
    from ``_check_ratio``'s image_to_video set — see _TEXT_VALID_RATIOS)."""
    ratio = params.get("ratio")
    if ratio is not None and ratio not in _TEXT_VALID_RATIOS:
        raise ProviderError(
            f"Invalid ratio={ratio!r} for Runway text-to-video. "
            f"Must be one of {set(_TEXT_VALID_RATIOS)}",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )


# Single family covering the Runway Gen *_turbo video catalog. The pattern
# ``^gen\w+_turbo$`` absorbs current (gen3a_turbo, gen4_turbo) and any
# future (gen4a_turbo, gen5_turbo, etc.) variants without a code change.
# Every matched slug is image-to-video only (see submit()'s routing) — the
# pinned SDK's text_to_video.create doesn't accept any of them as a `model`
# literal — so a text-only request to one raises the actionable
# _raise_image_required_error error. That capability is carried as
# ``extras["image_only"]`` (the framework's documented per-model escape
# hatch — see the Google Veo connector's ``extras["has_audio"]`` for the
# same pattern) rather than re-deriving it from the family's *parameter-shape*
# regex at submit() time, so the two concerns (param shape vs. endpoint
# capability) stay decoupled even though today they happen to coincide for
# every *_turbo slug. Constraints (duration ∈ {5, 10}, ratio ∈ _VALID_RATIOS)
# are Runway-wide rather than per-model, so they live on the family
# spec_template; the image-required check is NOT one of them — it's called
# explicitly from submit()'s routing decision (endpoint choice is
# per-request, not a fixed property of the spec). The ``ratio`` *default*
# is deliberately NOT set here either — it differs for gen3a_turbo (see
# ``_default_ratio_for_model``) and ``param_defaults`` can't express a
# per-model value across one shared template — submit() fills it in
# instead.
_RUNWAY_GEN_FAMILY = ModelFamily(
    name="runway-gen-video",
    pattern=re.compile(r"^gen\w+_turbo$"),
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_aliases={"aspect_ratio": "ratio"},
        param_constraints=(_check_duration, _check_ratio),
        input_mapping=route_images(slots=("prompt_image",)),
        extras={"image_only": True},
    ),
    description="Runway Gen video family (Gen-3a, Gen-4, future *_turbo variants).",
    example_slugs=("gen4_turbo", "gen3a_turbo"),
)


# Anything not matching the Gen-turbo family — including slugs the pinned
# SDK accepts but that don't fit the ``*_turbo`` pattern (gen4.5, veo3,
# veo3.1, veo3.1_fast) plus any future/unrecognized slug — falls back here.
# These models are text-capable (text_to_video.create accepts them), so
# ``extras["image_only"]`` is absent (falsy) — submit() routes them to
# image_to_video when an image is given and to text_to_video otherwise; no
# image requirement applies at the spec level. Duration/ratio *value*
# validation is intentionally skipped for both endpoints here (valid ranges
# differ per model, e.g. veo3's duration is fixed at 8s; submit-time errors
# are the authoritative signal — see ``DiscoverySupport.NONE`` above).
_FALLBACK = ModelSpec(
    model_id="*",
    modality=Modality.VIDEO,
    param_aliases={"aspect_ratio": "ratio"},
    input_mapping=route_images(slots=("prompt_image",)),
)


class RunwayProvider(BaseProvider):
    """Provider adapter for Runway video generation (Gen-3, Gen-4, veo3*).

    Models match the ``runway-gen-video`` family (any ``gen<N>[a]_turbo``
    slug — duration must be 5 or 10, ratio a pixel string like "1280:720")
    or fall back permissively to any other slug the pinned SDK accepts
    (``gen4.5``, ``veo3``, ``veo3.1``, ``veo3.1_fast``). ``submit()`` routes
    to Runway's ``image_to_video`` endpoint when the step has an input
    image (chained, via ``external_inputs=[image_asset]``, or
    ``params={'prompt_image': url}``) and to ``text_to_video`` otherwise.
    gen4_turbo/gen3a_turbo are image-to-video *only* — a text-only request
    to either raises a clear ``ProviderError`` rather than the SDK's
    generic "Missing required arguments" message (#226); every other model
    supports text-only prompts via ``text_to_video``.

    Auth: Set ``RUNWAYML_API_SECRET`` env var or pass ``api_secret``.

    Args:
        api_secret: Runway API secret. Falls back to ``RUNWAYML_API_SECRET``
            env var.
        poll_interval: Seconds between task status polls (default 5).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance TTL for the probe cache (unused for
            ``DiscoverySupport.NONE`` providers but accepted for ctor
            compatibility with sibling connectors).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "runway"
    discovery_support = DiscoverySupport.NONE
    """Runway has no ``GET /v1/models`` endpoint and the runwayml SDK
    doesn't expose raw HTTP for the empty-payload probe pattern. The
    catalog is small (2 models today) and stable — submit-time errors
    are the authoritative liveness signal. Pipeline preflight emits
    ``OK_PROVISIONAL`` for family-matched slugs."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_RUNWAY_GEN_FAMILY,),
            fallback=_FALLBACK,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """Runway: video generation from text and/or image inputs."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
            max_duration=10.0,
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        api_secret: str | None = None,
        poll_interval: float = 5.0,
        *,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ):
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self._api_secret = api_secret
        self._client: Any = None
        # Cache of in-progress task objects keyed by prediction_id, populated
        # in ``poll()`` and consumed in ``poll_progress()`` so we don't double
        # the API call rate just to surface preview/progress.
        self._progress_cache: dict[str, Any] = {}

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to Runway-native names.

        Kept for backward compatibility with callers that invoke it directly;
        ``prepare_payload`` also performs the alias via the model spec.
        """
        p = dict(params)
        if "aspect_ratio" in p and "ratio" not in p:
            p["ratio"] = p.pop("aspect_ratio")
        return p

    def _get_client(self):
        if self._client is None:
            try:
                from runwayml import RunwayML
            except ImportError as exc:
                raise ProviderError(
                    "runwayml package not installed. Run: pip install runwayml"
                ) from exc
            kwargs: dict = {}
            if self._api_secret:
                kwargs["api_key"] = self._api_secret
            self._client = RunwayML(**kwargs)
        return self._client

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Create a video generation task.

        Routes to Runway's ``image_to_video`` endpoint when the step has an
        input image (chained via step.inputs or supplied directly via
        ``params['prompt_image']``), or to ``text_to_video`` otherwise.
        gen4_turbo/gen3a_turbo are image-to-video only — the pinned SDK's
        ``text_to_video.create`` doesn't accept either as a ``model``
        literal — so a text-only request to one of those raises a clear
        error (#226) rather than routing to an endpoint that would reject
        the model outright.
        """
        client = self._get_client()
        try:
            payload = self.prepare_payload(step)
            has_image = bool(payload.get("prompt_image"))

            # Endpoint capability is a resolved-spec property
            # (extras["image_only"]), not re-derived from the family's
            # parameter-shape regex — see _RUNWAY_GEN_FAMILY's comment.
            is_image_only = bool(self._models.get(step.model).extras.get("image_only"))
            if not has_image and is_image_only:
                # e.g. gen4_turbo/gen3a_turbo: image-to-video only, no
                # image given — this is the only place that can be reached
                # with is_image_only True and no image, so it always raises.
                _raise_image_required_error(step.model)

            if has_image:
                task = self._submit_image_to_video(client, step, payload)
            else:
                task = self._submit_text_to_video(client, step, payload)
            return task.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Runway submit failed: {exc}",
                error_code=map_runway_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def _submit_image_to_video(self, client: Any, step: Step, payload: dict[str, Any]) -> Any:
        """Submit via Runway's ``image_to_video`` endpoint (image input required).

        Note: the pinned SDK's ``image_to_video.create`` has no ``watermark``
        parameter at all (it's keyword-only with no ``**kwargs``), so it's
        deliberately not forwarded here — a ``params={'watermark': ...}``
        would otherwise raise an opaque ``TypeError`` wrapped as a generic
        "Runway submit failed" (exactly the class of error #226 set out to
        kill).
        """
        # Translate canonical 'prompt' to Runway's 'prompt_text'; only the
        # SDK-recognized keys are forwarded to image_to_video.create.
        request: dict = {
            "model": step.model,
            "prompt_text": payload.get("prompt", step.prompt or ""),
        }
        for key in ("duration", "ratio", "seed", "prompt_image"):
            if key in payload:
                request[key] = payload[key]
        # Model-aware default: gen3a_turbo's valid ratio set is disjoint
        # from every other model's, so the default can't live on the
        # shared ModelSpec (see _default_ratio_for_model docstring).
        request.setdefault("ratio", _default_ratio_for_model(step.model))
        # gen4.5/veo3 mark `duration` required on this endpoint; every other
        # model accepts an absent one (see _default_duration_for_image_model).
        default_duration = _default_duration_for_image_model(step.model)
        if default_duration is not None:
            request.setdefault("duration", default_duration)

        # prompt_image reaches here two ways: routed from step.inputs
        # (already SSRF-validated by validate_chain_input_url in
        # prepare_payload) or supplied directly via step.params — which
        # is NOT validated upstream. _raise_image_required_error's message
        # explicitly suggests the params={'prompt_image': ...} path, so
        # validate it here regardless of origin, and regardless of shape
        # (single URL/data-URI string, or a list of {"uri": ...} dicts for
        # multi-frame models) before it reaches the SDK.
        if "prompt_image" in request:
            _validate_prompt_image(request["prompt_image"])

        # Record what was actually submitted so fetch_output's duration
        # metadata (and any duration-keyed pricing) matches reality instead
        # of guessing 5s for a model that just got a real default applied.
        if "duration" in request:
            step.params.setdefault("duration", request["duration"])

        return client.image_to_video.create(**request)

    def _submit_text_to_video(self, client: Any, step: Step, payload: dict[str, Any]) -> Any:
        """Submit via Runway's ``text_to_video`` endpoint (no image input).

        Only reached for models ``submit()`` has already determined are not
        image-only (``extras["image_only"]`` unset — see ``_RUNWAY_GEN_FAMILY``
        and ``_FALLBACK``). The pinned SDK's ``text_to_video.create`` accepts
        gen4.5, veo3, veo3.1, veo3.1_fast; any other slug is forwarded
        permissively — submit-time errors are the authoritative signal (see
        ``DiscoverySupport.NONE`` above) — so a future text-capable model
        works without a code change.
        """
        _check_text_ratio(payload)
        request: dict = {
            "model": step.model,
            "prompt_text": payload.get("prompt", step.prompt or ""),
        }
        for key in ("duration", "ratio", "seed"):
            if key in payload:
                request[key] = payload[key]
        # Valid ratio/duration value spaces differ from image_to_video's —
        # see _TEXT_VALID_RATIOS / _TEXT_DEFAULT_DURATION.
        request.setdefault("ratio", _TEXT_DEFAULT_RATIO)
        request.setdefault("duration", _TEXT_DEFAULT_DURATION)

        # Record what was actually submitted — duration is always resolved
        # here (defaulted if the caller omitted it), so fetch_output's
        # duration metadata reflects the real submission instead of its own
        # image_to_video-oriented 5s fallback.
        step.params.setdefault("duration", request["duration"])

        return client.text_to_video.create(**request)

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if the Runway task is complete."""
        client = self._get_client()
        try:
            task = client.tasks.retrieve(prediction_id)
            if task.status in ("SUCCEEDED", "FAILED"):
                self._cache_poll_result(prediction_id, task)
                return True
            # Stash the in-progress task so poll_progress() can read it
            # without a second API call.
            self._progress_cache[str(prediction_id)] = task
            return False
        except Exception as exc:
            raise ProviderError(
                f"Runway poll failed: {exc}",
                error_code=map_runway_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def poll_progress(self, prediction_id: Any) -> dict[str, Any] | None:
        """Surface Runway task ``progress`` and any preview thumbnail.

        Reads the in-progress task cached by the most recent ``poll()`` so
        we don't hit the API twice per tick. Returns None when no task has
        been cached yet (first poll attempt) or when neither field is set.
        """
        task = self._progress_cache.get(str(prediction_id))
        if task is None:
            return None
        signals: dict[str, Any] = {}
        progress = getattr(task, "progress", None)
        if isinstance(progress, (int, float)) and 0 <= progress <= 1:
            signals["progress_pct"] = float(progress)
        # Runway's task object exposes ``thumbnail_url`` on some Gen-4 models
        # for in-progress draft frames; getattr is defensive against SDK
        # versions that don't carry the field.
        preview = getattr(task, "thumbnail_url", None) or getattr(task, "preview_url", None)
        if preview:
            signals["preview_url"] = str(preview)
        return signals or None

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch the completed video URL."""
        client = self._get_client()
        try:
            task = self._get_cached_poll_result(prediction_id)
            if task is None:
                task = client.tasks.retrieve(prediction_id)

            step.provider_payload = {
                "runway": {
                    "task_id": task.id,
                    "status": task.status,
                }
            }

            if task.status == "FAILED":
                error_msg = getattr(task, "failure", None) or "Video generation failed"
                raise ProviderError(
                    str(error_msg),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            # Task output contains the video URL
            output = getattr(task, "output", None)
            if output and isinstance(output, list) and len(output) > 0:
                url = str(output[0])
                validate_asset_url(url)
                step.assets.append(Asset(url=url, media_type="video/mp4"))
            elif output and isinstance(output, str):
                validate_asset_url(output)
                step.assets.append(Asset(url=output, media_type="video/mp4"))
            else:
                raise ProviderError("Runway task completed but no output URL found")

            # Default duration is 5s when the user didn't specify one — kept
            # so VideoMetadata.duration is populated consistently regardless
            # of whether the caller supplied the param. (Pricing was
            # previously also keyed off this; no longer SDK state.)
            duration = int(step.params.get("duration", 5))
            step.params.setdefault("duration", duration)
            for a in step.assets:
                a.video = VideoMetadata(has_audio=False)
                a.duration = a.duration or float(duration)

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Runway fetch_output failed: {exc}",
                error_code=map_runway_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
