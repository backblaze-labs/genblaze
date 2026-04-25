"""Base provider — abstract interface for media generation APIs."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from genblaze_core._utils import _SECRET_PATTERNS, jittered_backoff, new_id, utc_now
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import (
    RETRYABLE_ERROR_CODES,
    Modality,
    ProviderErrorCode,
    StepStatus,
)
from genblaze_core.models.step import Step
from genblaze_core.observability.span import StepSpan
from genblaze_core.providers.model_registry import EMPTY_REGISTRY, ModelRegistry, compute_cost
from genblaze_core.providers.pricing import PricingContext
from genblaze_core.providers.probe import ProbeResult
from genblaze_core.providers.progress import ProgressEvent
from genblaze_core.providers.retry import PRE_RESPONSE_EXCEPTIONS
from genblaze_core.providers.spec import ModelSpec
from genblaze_core.runnable.base import Runnable
from genblaze_core.runnable.config import RunnableConfig

if TYPE_CHECKING:
    from genblaze_core.models.voice import Voice

logger = logging.getLogger("genblaze.provider")

# Env-var escape hatch for offline tests / fixtures that mock submit() without
# also mocking the preflight endpoint.
_SKIP_PREFLIGHT_ENV = "GENBLAZE_SKIP_PREFLIGHT"

# Default poll interval and timeout for the submit→poll→fetch lifecycle
DEFAULT_POLL_INTERVAL = 1.0  # seconds
DEFAULT_TIMEOUT = 600.0  # 10 minutes

# Max error message length stored in step.error (prevents bloated manifests)
_MAX_ERROR_LENGTH = 500

# Max consecutive transient poll() errors tolerated inside a single invoke().
# Guards against misclassification in connectors that wrap httpx/boto exceptions
# opaquely — a single 503 mid-poll shouldn't fail a 10-minute video generation.
# Overridable per-provider via subclass attribute if needed.
_DEFAULT_POLL_TRANSIENT_RETRIES = 5


@dataclass
class ProviderCapabilities:
    """Declares what a provider supports for upfront validation and discovery.

    All fields are optional — omitting a field means "unspecified" (no restriction).
    """

    supported_modalities: list[Modality] | None = field(default=None)
    supported_inputs: list[str] | None = field(default=None)  # e.g. ["text", "image", "video"]
    accepts_chain_input: bool = field(default=False)  # True if provider reads step.inputs
    max_duration: float | None = field(default=None)  # seconds
    resolutions: list[str] | None = field(default=None)  # e.g. ["720p", "1080p", "4k"]
    output_formats: list[str] | None = field(default=None)  # e.g. ["video/mp4", "audio/mpeg"]
    models: list[str] | None = field(default=None)  # known model IDs


@dataclass
class SubmitResult:
    """Result from provider submit(), with optional timing hints for adaptive polling.

    Providers can return this instead of a raw prediction ID to give the
    polling loop an estimated completion time, reducing unnecessary API calls.
    """

    prediction_id: Any
    estimated_seconds: float | None = field(default=None)


def _adaptive_poll_interval(elapsed: float, base: float, max_interval: float = 30.0) -> float:
    """Compute poll interval that backs off as elapsed time increases.

    Starts at base, doubles every 30s of elapsed time, capped at max_interval.
    """
    doublings = int(elapsed / 30)
    return min(base * (2**doublings), max_interval)


def _sanitize_error(msg: str) -> str:
    """Redact potential secrets and truncate error messages for safe storage."""
    sanitized = _SECRET_PATTERNS.sub("[REDACTED]", msg)
    if len(sanitized) > _MAX_ERROR_LENGTH:
        sanitized = sanitized[:_MAX_ERROR_LENGTH] + "...(truncated)"
    return sanitized


def classify_api_error(exc: Exception | str) -> ProviderErrorCode:
    """Map an exception to a normalized ProviderErrorCode.

    Shared default error classifier for provider adapters. Connectors with
    provider-specific error types (gRPC codes, HTTP status ints, SDK exceptions)
    should keep their own mapper; connectors that only do string matching can
    delegate here.
    """
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return ProviderErrorCode.TIMEOUT
    if "rate limit" in msg or "rate_limit" in msg or "429" in msg:
        return ProviderErrorCode.RATE_LIMIT
    # Content policy / safety refusal — deterministic, never retryable.
    # Check before auth/invalid because a refusal often reads as 400 and
    # carries "policy" / "safety" in the message.
    policy_terms = (
        "content_policy",
        "content policy",
        "safety_filter",
        "safety filter",
        "content filter",
        "policy violation",
        "blocked by safety",
        "responsibleai",
    )
    if any(t in msg for t in policy_terms):
        return ProviderErrorCode.CONTENT_POLICY
    auth_terms = ("auth", "unauthorized", "forbidden", "401", "403", "api_key")
    if any(t in msg for t in auth_terms):
        return ProviderErrorCode.AUTH_FAILURE
    if "invalid" in msg or "validation" in msg or "400" in msg:
        return ProviderErrorCode.INVALID_INPUT
    # Check server errors before model errors — "model" appears in many server messages
    if "server" in msg or "500" in msg or "502" in msg or "503" in msg:
        return ProviderErrorCode.SERVER_ERROR
    if "model" in msg and ("not found" in msg or "not available" in msg):
        return ProviderErrorCode.MODEL_ERROR
    return ProviderErrorCode.UNKNOWN


# Allowed URL schemes for asset URLs — shared across all providers
_ALLOWED_SCHEMES = {"https"}


def validate_asset_url(url: str) -> None:
    """Reject non-HTTPS or malformed URLs to prevent SSRF.

    All providers should call this when attaching asset URLs from API responses.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        raise ProviderError(f"Unsafe asset URL '{url}' — only absolute HTTPS URLs allowed")


# Schemes allowed for chain inputs (file:// from local providers, https:// from cloud)
_CHAIN_INPUT_SCHEMES = frozenset({"https", "file"})


def validate_chain_input_url(url: str) -> None:
    """Validate a URL from step.inputs before forwarding to a provider.

    Allows file:// (local chain outputs from SyncProviders) and https://
    (cloud-hosted assets). Rejects http:// and other schemes.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _CHAIN_INPUT_SCHEMES:
        raise ProviderError(
            f"Unsafe chain input URL '{url}' — only HTTPS and file:// URLs allowed"
        )
    if parsed.scheme == "https" and not parsed.netloc:
        raise ProviderError(f"Malformed HTTPS URL '{url}' — missing host")


class BaseProvider(Runnable[Step, Step]):
    """Abstract base for all provider adapters.

    Providers implement the 3-method lifecycle:
    1. submit — send the generation request
    2. poll — check for completion
    3. fetch_output — retrieve results and attach assets

    Subclasses may override ``create_registry()`` to declare per-model specs
    (pricing, parameter aliases, input routing, validation). The registry is
    consulted in two places:

    - ``prepare_payload(step)`` — run the full parameter pipeline (aliases →
      transformer → chain inputs → coercers → defaults → schemas → required →
      constraints → allowlist) before submit.
    - After ``fetch_output()`` — if a spec defines ``pricing`` and
      ``step.cost_usd`` is not already set, compute it automatically.

    Users customize the registry via the ``models=`` init kwarg or by mutating
    the class-level default returned from ``models_default()``.
    """

    name: str = "base"
    poll_interval: float = DEFAULT_POLL_INTERVAL
    # Max transient failures tolerated per phase (submit/poll/fetch) before escalating.
    # Counter is phase-local — a successful poll does not refund submit's budget.
    # Set to 0 to disable intra-phase retries.
    poll_transient_retries: int = _DEFAULT_POLL_TRANSIENT_RETRIES

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Ensure each subclass has its own cache slot — avoids inheriting a
        # sibling class's registry.
        super().__init_subclass__(**kwargs)
        cls._models_cache = None  # type: ignore[attr-defined]

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        """Return the package-default ModelRegistry for this provider.

        Override in subclasses to declare specs. Default is the empty,
        permissive registry — matches historical "pass everything, no pricing"
        behavior.
        """
        return EMPTY_REGISTRY

    @classmethod
    def models_default(cls) -> ModelRegistry:
        """Class-level registry, built lazily once per subclass."""
        # Check __dict__ so we don't read a parent class's cache
        if cls.__dict__.get("_models_cache") is None:
            cls._models_cache = cls.create_registry()  # type: ignore[attr-defined]
        return cls._models_cache  # type: ignore[attr-defined,return-value]

    def __init__(self, *, models: ModelRegistry | None = None) -> None:
        # Poll result cache — avoids redundant API calls between poll() and fetch_output()
        self._poll_cache: dict[str, Any] = {}
        self._poll_cache_times: dict[str, float] = {}
        self._poll_cache_max_age: float = 3600.0  # 1 hour TTL
        # Use ``is not None`` rather than truthiness — ``ModelRegistry.__len__``
        # makes an empty registry falsy, so ``models or default`` would silently
        # discard explicit empty overrides (Replicate / LMNT have empty defaults).
        self._models: ModelRegistry = models if models is not None else type(self).models_default()
        # One-shot preflight gate — credentials checked before the first submit
        # of this instance, then skipped for the lifetime of the object.
        # The locks are lazy: ``threading.Lock`` is cheap to construct here, but
        # ``asyncio.Lock`` requires a running loop, so it's deferred to first
        # async use via ``_get_async_preflight_lock``.
        self._preflight_done: bool = False
        self._preflight_sync_lock: threading.Lock = threading.Lock()
        self._preflight_async_lock: asyncio.Lock | None = None

    def _cache_poll_result(self, prediction_id: Any, result: Any) -> None:
        """Cache a poll result for reuse in fetch_output()."""
        key = str(prediction_id)
        self._poll_cache[key] = result
        self._poll_cache_times[key] = time.monotonic()

    def _get_cached_poll_result(self, prediction_id: Any) -> Any | None:
        """Return cached poll result if available. Consumes the entry."""
        key = str(prediction_id)
        result = self._poll_cache.pop(key, None)
        self._poll_cache_times.pop(key, None)
        return result

    def _cleanup_poll_cache(self) -> None:
        """Remove poll cache entries older than TTL to prevent memory leaks."""
        now = time.monotonic()
        max_age = self._poll_cache_max_age
        # Snapshot first — a concurrent poll() running in asyncio.to_thread
        # can call _cache_poll_result mid-iteration and raise
        # "RuntimeError: dictionary changed size during iteration".
        snapshot = list(self._poll_cache_times.items())
        stale = [k for k, t in snapshot if now - t > max_age]
        for k in stale:
            self._poll_cache.pop(k, None)
            self._poll_cache_times.pop(k, None)

    @property
    def models(self) -> ModelRegistry:
        """The per-instance ``ModelRegistry`` (class default unless overridden)."""
        return self._models

    # --- discovery / catalog hooks -----------------------------------------

    def list_models(self) -> list[ModelSpec]:
        """Return every registered ``ModelSpec`` for this provider instance.

        Convenience over ``provider.models.items()`` for app-side discovery
        (model pickers, cost dashboards, capability matrices). Sorted by
        ``model_id`` for deterministic output.
        """
        return [spec for _, spec in self._models.items()]

    def list_voices(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> list[Voice]:
        """Return available voices for TTS / music models. Default empty.

        Audio connectors override this to return either a curated catalog
        (GMI, OpenAI TTS, NVIDIA Riva) or a live-API fetch with caching
        (ElevenLabs, LMNT). Non-audio connectors leave the default in place.

        Filters are advisory — implementations should return only voices that
        match both ``model`` (when supplied) and ``language`` (BCP 47 prefix
        match, e.g. ``"en"`` matches ``"en-US"``).
        """
        return []

    # --- pre-flight + probe contracts --------------------------------------

    def preflight_auth(self, *, timeout: float = 5.0) -> None:
        """Cheap credential check called once per instance before the first submit.

        Default is a no-op so connectors that haven't opted in keep working.
        Override with a fast (sub-second) call against a known-cheap endpoint
        to surface bad credentials immediately rather than after a long-running
        ``submit()`` blocks for the full HTTP timeout.

        Implementations should raise ``ProviderError`` (preferably with
        ``error_code=AUTH_FAILURE``) on credential rejection, and let
        transient/network errors surface naturally — the calling site treats
        any exception as a hard preflight failure.

        Disabled when ``GENBLAZE_SKIP_PREFLIGHT`` is set (offline test escape).
        """
        return None

    def probe_model(self, model_id: str) -> ProbeResult:
        """Cheap liveness check for a single model id. Default ``SKIPPED``.

        Used by ``tools/probe_models.py`` to detect drift between a connector's
        registry defaults and its upstream API. Connectors with a cheap catalog
        endpoint (``GET /models``) intersect against the live list; queue-style
        connectors POST a deliberately-empty payload and distinguish 404 from
        400. See :class:`~genblaze_core.providers.probe.ProbeResult`.
        """
        return ProbeResult.skipped()

    # --- pricing estimation ------------------------------------------------

    def estimate_cost(
        self,
        model: str,
        params: Mapping[str, Any] | None = None,
        *,
        n: int = 1,
    ) -> Decimal | None:
        """Compute upfront USD cost for ``n`` outputs without running the model.

        Returns ``None`` when:
        - the model is unknown or has no registered ``pricing`` strategy, or
        - the strategy depends on response-only data (e.g. per-byte costs that
          require an actual asset), in which case the caller falls back to
          "varies."

        Synthesizes a minimal ``Step`` + ``n`` placeholder ``Asset`` instances
        so existing per-unit / per-second / param-based pricing strategies work
        unchanged. Asset ``duration`` is populated from ``params["duration"]``
        when present, so per-second video pricing estimates correctly.
        """
        spec = self._models.get(model)
        if spec is None or spec.pricing is None:
            return None
        params = dict(params or {})
        fake_step = Step(
            provider=self.name,
            model=model,
            params=params,
            prompt=str(params.get("prompt") or ""),
        )
        duration = params.get("duration")
        try:
            duration_f = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_f = None
        fake_assets = tuple(
            Asset(
                asset_id=new_id(),
                url=f"about:blank#{i}",
                media_type="application/octet-stream",
                duration=duration_f,
            )
            for i in range(n)
        )
        cost = spec.pricing(PricingContext(step=fake_step, assets=fake_assets))
        if cost is None:
            return None
        return Decimal(str(cost))

    # --- capability declaration --------------------------------------------

    def get_capabilities(self) -> ProviderCapabilities | None:
        """Return provider capabilities for discovery and validation.

        Override in subclasses to declare supported modalities, inputs,
        resolutions, etc. Returns None by default (unspecified).
        """
        return None

    def normalize_params(
        self, params: dict[str, Any], modality: Modality | None = None
    ) -> dict[str, Any]:
        """Normalize standard parameter names to provider-specific ones.

        Override in subclasses to map standard params (duration, resolution,
        aspect_ratio) to provider-native names. Default returns params unchanged.
        Native params always take precedence over standard ones.

        Runs *before* the ``ModelSpec``-driven pipeline in ``prepare_payload``.
        """
        return params

    def prepare_payload(
        self,
        step: Step,
        *,
        base_params: dict[str, Any] | None = None,
        validate_inputs: bool = True,
    ) -> dict[str, Any]:
        """Run the registered ``ModelSpec`` pipeline for ``step.model``.

        Returns the dict to forward to the provider SDK. SSRF-validates every
        ``step.inputs`` URL before the spec's ``input_mapping`` reads them,
        unless ``validate_inputs=False`` (connectors that do their own
        validation can opt out).
        """
        if validate_inputs:
            for asset in step.inputs:
                validate_chain_input_url(asset.url)
        if base_params is None:
            base_params = {}
            if step.prompt is not None:
                base_params["prompt"] = step.prompt
            if step.negative_prompt is not None:
                base_params["negative_prompt"] = step.negative_prompt
            if step.seed is not None:
                base_params["seed"] = step.seed
            base_params.update(step.params)
        base_params = self.normalize_params(base_params, step.modality)
        return self._models.prepare_payload(step, base_params=base_params)

    def _apply_registry_pricing(self, step: Step) -> None:
        """If spec pricing is defined and cost not already set, compute and attach."""
        if step.cost_usd is not None:
            return
        cost = compute_cost(self._models, step)
        if cost is not None:
            step.cost_usd = cost

    def _fire_progress(
        self,
        step: Step,
        config: RunnableConfig | None,
        status: str,
        start_time: float,
        progress_pct: float | None = None,
        message: str | None = None,
        preview_url: str | None = None,
    ) -> None:
        """Fire on_progress callback if one is configured."""
        callback = (config or {}).get("on_progress")
        if callback is not None:
            callback(
                ProgressEvent(
                    step_id=step.step_id,
                    provider=self.name,
                    model=step.model,
                    status=status,
                    progress_pct=progress_pct,
                    elapsed_sec=time.monotonic() - start_time,
                    message=message,
                    preview_url=preview_url,
                )
            )

    @abstractmethod
    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit the generation request. Returns a provider-specific prediction ID."""
        ...

    @abstractmethod
    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Poll for completion. Returns True when done."""
        ...

    @abstractmethod
    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Fetch results and attach assets to the step. Returns updated step."""
        ...

    @staticmethod
    def _classify_poll_exc(exc: Exception) -> ProviderErrorCode:
        """Pick the best error code for an exception raised by poll().

        Prefers an explicit ProviderError.error_code over string-matching so
        connectors that already categorize their SDK exceptions keep the
        classification they set.
        """
        if isinstance(exc, ProviderError) and exc.error_code is not None:
            return exc.error_code
        return classify_api_error(exc)

    @staticmethod
    def _is_retryable(exc: Exception, retry_on: tuple[type[BaseException], ...] | None) -> bool:
        """Retry rule for one phase.

        When ``retry_on`` is supplied (submit phase), only those exception
        classes are eligible — this is how we constrain submit retries to
        pre-response network failures where replay is safe without an
        idempotency key. Otherwise (poll/fetch) we fall back to the normalized
        error code.
        """
        if retry_on is not None:
            return isinstance(exc, retry_on)
        return BaseProvider._classify_poll_exc(exc) in RETRYABLE_ERROR_CODES

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        """Delay before the next attempt — server hint wins over computed backoff."""
        if isinstance(exc, ProviderError) and exc.retry_after is not None:
            return exc.retry_after
        return jittered_backoff(attempt)

    def _emit_retry(
        self,
        step: Step,
        config: RunnableConfig | None,
        phase: Literal["submit", "poll", "fetch"],
        exc: Exception,
        attempt: int,
        delay: float,
    ) -> None:
        """Log + fire ``on_retry`` so pipeline streams can surface the retry."""
        code = self._classify_poll_exc(exc)
        logger.warning(
            "%s %s retry %d/%d in %.1fs (code=%s)",
            self.name,
            phase,
            attempt,
            self.poll_transient_retries,
            delay,
            code,
        )
        callback = (config or {}).get("on_retry")
        if callback is None:
            return
        # Local import to avoid a circular reference at module import time.
        from genblaze_core.observability.events import StepRetriedEvent

        callback(
            StepRetriedEvent(
                run_id=(config or {}).get("run_id"),
                step_id=step.step_id,
                provider=self.name,
                model=step.model,
                phase=phase,
                attempt=attempt,
                max_attempts=self.poll_transient_retries + 1,
                delay_sec=delay,
                error_code=str(code) if code else None,
                error=_sanitize_error(str(exc)),
            )
        )

    def _retry_phase(
        self,
        fn: Any,
        *,
        phase: Literal["submit", "poll", "fetch"],
        step: Step,
        config: RunnableConfig | None,
        start_time: float,
        timeout: float,
        retry_on: tuple[type[BaseException], ...] | None = None,
    ) -> Any:
        """Run ``fn()`` with retries; unified across submit / poll / fetch.

        ``retry_on=PRE_RESPONSE_EXCEPTIONS`` narrows submit retries to network
        errors that cannot have triggered a side effect. Everywhere else the
        retryable set is driven by the normalized error code.
        """
        max_budget = self.poll_transient_retries
        attempt = 1
        while True:
            try:
                return fn()
            except Exception as exc:
                if not self._is_retryable(exc, retry_on) or attempt > max_budget:
                    if isinstance(exc, ProviderError):
                        exc.attempts = attempt
                    raise
                delay = self._retry_delay(exc, attempt)
                elapsed = time.monotonic() - start_time
                if elapsed + delay >= timeout:
                    if isinstance(exc, ProviderError):
                        exc.attempts = attempt
                    raise
                self._emit_retry(step, config, phase, exc, attempt, delay)
                time.sleep(delay)
                attempt += 1

    async def _aretry_phase(
        self,
        fn: Any,
        *,
        phase: Literal["submit", "poll", "fetch"],
        step: Step,
        config: RunnableConfig | None,
        start_time: float,
        timeout: float,
        retry_on: tuple[type[BaseException], ...] | None = None,
    ) -> Any:
        """Async twin of ``_retry_phase`` — ``fn`` is an awaitable factory."""
        max_budget = self.poll_transient_retries
        attempt = 1
        while True:
            try:
                return await fn()
            except Exception as exc:
                if not self._is_retryable(exc, retry_on) or attempt > max_budget:
                    if isinstance(exc, ProviderError):
                        exc.attempts = attempt
                    raise
                delay = self._retry_delay(exc, attempt)
                elapsed = time.monotonic() - start_time
                if elapsed + delay >= timeout:
                    if isinstance(exc, ProviderError):
                        exc.attempts = attempt
                    raise
                self._emit_retry(step, config, phase, exc, attempt, delay)
                await asyncio.sleep(delay)
                attempt += 1

    def _run_preflight_once(self) -> None:
        """Run ``preflight_auth`` once per instance, honoring the env-var skip.

        Idempotent and thread-safe: concurrent submit() calls (e.g. from
        ``ThreadPoolExecutor`` batches) will only invoke ``preflight_auth``
        once. The flag is set even when the check raises, so a permanent
        auth failure doesn't get retried on every submit (the calling site
        already surfaced the error).
        """
        if self._preflight_done or os.environ.get(_SKIP_PREFLIGHT_ENV):
            return
        # Double-checked locking — cheap fast-path read, lock only when needed.
        with self._preflight_sync_lock:
            if self._preflight_done:
                return
            try:
                self.preflight_auth()
            finally:
                self._preflight_done = True

    def _get_async_preflight_lock(self) -> asyncio.Lock:
        """Lazily create the asyncio lock on the running loop's first call."""
        if self._preflight_async_lock is None:
            self._preflight_async_lock = asyncio.Lock()
        return self._preflight_async_lock

    async def _arun_preflight_once(self) -> None:
        """Async twin of ``_run_preflight_once`` — protects concurrent coroutines.

        Without this, two coroutines that pass the unlocked ``_preflight_done``
        check on the same loop both dispatch ``asyncio.to_thread`` and run
        ``preflight_auth`` in parallel.
        """
        if self._preflight_done or os.environ.get(_SKIP_PREFLIGHT_ENV):
            return
        async with self._get_async_preflight_lock():
            if self._preflight_done:
                return
            await asyncio.to_thread(self._run_preflight_once)

    def _attempt_once(
        self, step: Step, config: RunnableConfig | None, timeout: float, start_time: float
    ) -> Step:
        """Execute a single submit→poll→fetch attempt with adaptive polling."""
        self._cleanup_poll_cache()
        self._run_preflight_once()
        step.started_at = utc_now()
        step.status = StepStatus.SUBMITTED
        self._fire_progress(step, config, "submitted", start_time)

        logger.debug("Submitting to %s: model=%s", self.name, step.model)
        raw = self._retry_phase(
            lambda: self.submit(step, config),
            phase="submit",
            step=step,
            config=config,
            start_time=start_time,
            timeout=timeout,
            retry_on=PRE_RESPONSE_EXCEPTIONS,
        )

        # Support SubmitResult for timing hints (backward compatible with plain IDs)
        if isinstance(raw, SubmitResult):
            prediction_id = raw.prediction_id
            estimated_seconds = raw.estimated_seconds
        else:
            prediction_id = raw
            estimated_seconds = None

        # Fire checkpoint callback so callers can persist prediction_id before polling
        on_submit = (config or {}).get("on_submit")
        if on_submit is not None:
            on_submit(step.step_id, prediction_id)

        step.status = StepStatus.PROCESSING

        # If provider gave a time estimate, delay first poll to reduce API calls
        if estimated_seconds is not None and estimated_seconds > 0:
            initial_delay = min(
                estimated_seconds * 0.8,
                timeout - (time.monotonic() - start_time),
            )
            if initial_delay > 0:
                time.sleep(initial_delay)

        while True:
            done = self._retry_phase(
                lambda: self.poll(prediction_id, config),
                phase="poll",
                step=step,
                config=config,
                start_time=start_time,
                timeout=timeout,
            )
            if done:
                break
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise ProviderError(f"Poll timeout after {elapsed:.1f}s (limit: {timeout}s)")
            self._fire_progress(step, config, "processing", start_time)
            interval = _adaptive_poll_interval(elapsed, self.poll_interval)
            time.sleep(interval)

        step = self._retry_phase(
            lambda: self.fetch_output(prediction_id, step),
            phase="fetch",
            step=step,
            config=config,
            start_time=start_time,
            timeout=timeout,
        )
        self._apply_registry_pricing(step)
        # Only mark succeeded if fetch_output didn't signal failure
        if step.status != StepStatus.FAILED:
            step.status = StepStatus.SUCCEEDED
            step.completed_at = utc_now()
        self._fire_progress(step, config, "succeeded", start_time)
        logger.debug("Step succeeded: %d assets", len(step.assets))
        return step

    def _finalize_resume_step(
        self,
        step: Step,
        config: RunnableConfig | None,
        start_time: float,
    ) -> Step:
        """Set final status and fire progress after fetch_output in resume paths."""
        if step.status != StepStatus.FAILED:
            step.status = StepStatus.SUCCEEDED
            step.completed_at = utc_now()
            self._fire_progress(step, config, "succeeded", start_time)
        else:
            self._fire_progress(step, config, "failed", start_time)
        return step

    def _handle_resume_error(
        self,
        step: Step,
        exc: Exception,
        config: RunnableConfig | None,
        start_time: float,
    ) -> Step:
        """Classify and record an error during resume — shared by sync/async paths."""
        error_code = (
            exc.error_code
            if isinstance(exc, ProviderError) and exc.error_code is not None
            else classify_api_error(exc)
        )
        step.status = StepStatus.FAILED
        step.error = _sanitize_error(str(exc))
        step.error_code = error_code
        step.completed_at = utc_now()
        self._fire_progress(step, config, "failed", start_time)
        logger.warning("Resume failed: %s (code=%s)", step.error, step.error_code)
        return step

    def resume(
        self,
        prediction_id: Any,
        step: Step,
        config: RunnableConfig | None = None,
    ) -> Step:
        """Resume polling an in-flight job, skipping submit().

        Use this to recover from a worker restart during a long-running
        generation. Default implementation polls until done, then fetches output.
        Errors are classified and recorded on the step (matching invoke() behavior).
        """
        step = step.model_copy()
        timeout = (config or {}).get("timeout", DEFAULT_TIMEOUT)
        start_time = time.monotonic()

        step.status = StepStatus.PROCESSING
        if step.started_at is None:
            step.started_at = utc_now()
        self._fire_progress(step, config, "resumed", start_time)

        try:
            while True:
                done = self._retry_phase(
                    lambda: self.poll(prediction_id, config),
                    phase="poll",
                    step=step,
                    config=config,
                    start_time=start_time,
                    timeout=timeout,
                )
                if done:
                    break
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    msg = f"Resume poll timeout after {elapsed:.1f}s (limit: {timeout}s)"
                    raise ProviderError(msg)
                self._fire_progress(step, config, "processing", start_time)
                interval = _adaptive_poll_interval(elapsed, self.poll_interval)
                time.sleep(interval)

            step = self._retry_phase(
                lambda: self.fetch_output(prediction_id, step),
                phase="fetch",
                step=step,
                config=config,
                start_time=start_time,
                timeout=timeout,
            )
            self._apply_registry_pricing(step)
            return self._finalize_resume_step(step, config, start_time)
        except Exception as exc:
            return self._handle_resume_error(step, exc, config, start_time)

    async def aresume(
        self,
        prediction_id: Any,
        step: Step,
        config: RunnableConfig | None = None,
    ) -> Step:
        """Async version of resume() — polls without blocking the event loop.

        Errors are classified and recorded on the step (matching ainvoke() behavior).
        """
        step = step.model_copy()
        timeout = (config or {}).get("timeout", DEFAULT_TIMEOUT)
        start_time = time.monotonic()

        step.status = StepStatus.PROCESSING
        if step.started_at is None:
            step.started_at = utc_now()
        self._fire_progress(step, config, "resumed", start_time)

        try:
            while True:
                done = await self._aretry_phase(
                    lambda: asyncio.to_thread(self.poll, prediction_id, config),
                    phase="poll",
                    step=step,
                    config=config,
                    start_time=start_time,
                    timeout=timeout,
                )
                if done:
                    break
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    msg = f"Resume poll timeout after {elapsed:.1f}s (limit: {timeout}s)"
                    raise ProviderError(msg)
                self._fire_progress(step, config, "processing", start_time)
                interval = _adaptive_poll_interval(elapsed, self.poll_interval)
                await asyncio.sleep(interval)

            step = await self._aretry_phase(
                lambda: asyncio.to_thread(self.fetch_output, prediction_id, step),
                phase="fetch",
                step=step,
                config=config,
                start_time=start_time,
                timeout=timeout,
            )
            self._apply_registry_pricing(step)
            return self._finalize_resume_step(step, config, start_time)
        except Exception as exc:
            return self._handle_resume_error(step, exc, config, start_time)

    def invoke(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Execute the full submit→poll→fetch lifecycle with optional retry."""
        step = step.model_copy()
        timeout = (config or {}).get("timeout", DEFAULT_TIMEOUT)
        max_retries = (config or {}).get("max_retries", 0)
        start_time = time.monotonic()

        span = StepSpan(name=f"{self.name}/{step.model}", step_id=step.step_id)

        with span:
            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        # Reset step state for retry
                        step.status = StepStatus.PENDING
                        step.error = None
                        step.error_code = None
                        step.assets = []

                    step = self._attempt_once(step, config, timeout, start_time)
                    span.retries = step.retries
                    # Copy cost from span to step if provider set it
                    if span.cost is not None:
                        step.cost_usd = span.cost
                    return step

                except Exception as exc:
                    error_code = (
                        exc.error_code
                        if isinstance(exc, ProviderError) and exc.error_code is not None
                        else classify_api_error(exc)
                    )

                    # Only retry on transient errors
                    if error_code not in RETRYABLE_ERROR_CODES or attempt >= max_retries:
                        step.status = StepStatus.FAILED
                        step.error = _sanitize_error(str(exc))
                        step.error_code = error_code
                        step.completed_at = utc_now()
                        self._fire_progress(step, config, "failed", start_time)
                        logger.warning("Step failed: %s (code=%s)", step.error, step.error_code)
                        span.retries = step.retries
                        return step

                    step.retries += 1
                    backoff = jittered_backoff(attempt)
                    logger.info(
                        "Retry %d/%d after %s (backoff=%.1fs)",
                        attempt + 1,
                        max_retries,
                        error_code,
                        backoff,
                    )

                    # Check global timeout before sleeping
                    elapsed = time.monotonic() - start_time
                    if elapsed + backoff >= timeout:
                        step.status = StepStatus.FAILED
                        step.error = _sanitize_error(str(exc))
                        step.error_code = error_code
                        step.completed_at = utc_now()
                        self._fire_progress(step, config, "failed", start_time)
                        logger.warning("Retry aborted: global timeout would be exceeded")
                        span.retries = step.retries
                        return step

                    time.sleep(backoff)

        # Should not reach here, but safety fallback
        return step  # pragma: no cover

    async def _attempt_once_async(
        self, step: Step, config: RunnableConfig | None, timeout: float, start_time: float
    ) -> Step:
        """Execute a single submit→poll→fetch attempt without blocking the event loop."""
        self._cleanup_poll_cache()
        if not self._preflight_done and not os.environ.get(_SKIP_PREFLIGHT_ENV):
            if type(self).preflight_auth is BaseProvider.preflight_auth:
                # Default no-op — set the flag inline; no need to spawn a thread
                # (and avoid the context switch that would reorder concurrent
                # steps in tests using cooperative ``asyncio.gather`` ordering).
                self._preflight_done = True
            else:
                # Custom preflight does I/O; the locked async runner ensures
                # concurrent coroutines on the same loop only invoke it once.
                await self._arun_preflight_once()
        step.started_at = utc_now()
        step.status = StepStatus.SUBMITTED
        self._fire_progress(step, config, "submitted", start_time)

        logger.debug("Submitting to %s: model=%s", self.name, step.model)
        raw = await self._aretry_phase(
            lambda: asyncio.to_thread(self.submit, step, config),
            phase="submit",
            step=step,
            config=config,
            start_time=start_time,
            timeout=timeout,
            retry_on=PRE_RESPONSE_EXCEPTIONS,
        )

        # Support SubmitResult for timing hints (backward compatible with plain IDs)
        if isinstance(raw, SubmitResult):
            prediction_id = raw.prediction_id
            estimated_seconds = raw.estimated_seconds
        else:
            prediction_id = raw
            estimated_seconds = None

        # Fire checkpoint callback so callers can persist prediction_id before polling
        on_submit = (config or {}).get("on_submit")
        if on_submit is not None:
            on_submit(step.step_id, prediction_id)

        step.status = StepStatus.PROCESSING

        # If provider gave a time estimate, delay first poll to reduce API calls
        if estimated_seconds is not None and estimated_seconds > 0:
            initial_delay = min(
                estimated_seconds * 0.8,
                timeout - (time.monotonic() - start_time),
            )
            if initial_delay > 0:
                await asyncio.sleep(initial_delay)

        while True:
            done = await self._aretry_phase(
                lambda: asyncio.to_thread(self.poll, prediction_id, config),
                phase="poll",
                step=step,
                config=config,
                start_time=start_time,
                timeout=timeout,
            )
            if done:
                break
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise ProviderError(f"Poll timeout after {elapsed:.1f}s (limit: {timeout}s)")
            self._fire_progress(step, config, "processing", start_time)
            interval = _adaptive_poll_interval(elapsed, self.poll_interval)
            await asyncio.sleep(interval)

        step = await self._aretry_phase(
            lambda: asyncio.to_thread(self.fetch_output, prediction_id, step),
            phase="fetch",
            step=step,
            config=config,
            start_time=start_time,
            timeout=timeout,
        )
        self._apply_registry_pricing(step)
        if step.status != StepStatus.FAILED:
            step.status = StepStatus.SUCCEEDED
            step.completed_at = utc_now()
        self._fire_progress(step, config, "succeeded", start_time)
        logger.debug("Step succeeded: %d assets", len(step.assets))
        return step

    async def ainvoke(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Async submit→poll→fetch lifecycle — uses asyncio.sleep instead of blocking."""
        step = step.model_copy()
        timeout = (config or {}).get("timeout", DEFAULT_TIMEOUT)
        max_retries = (config or {}).get("max_retries", 0)
        start_time = time.monotonic()

        span = StepSpan(name=f"{self.name}/{step.model}", step_id=step.step_id)

        with span:
            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        step.status = StepStatus.PENDING
                        step.error = None
                        step.error_code = None
                        step.assets = []

                    step = await self._attempt_once_async(step, config, timeout, start_time)
                    span.retries = step.retries
                    if span.cost is not None:
                        step.cost_usd = span.cost
                    return step

                except Exception as exc:
                    error_code = (
                        exc.error_code
                        if isinstance(exc, ProviderError) and exc.error_code is not None
                        else classify_api_error(exc)
                    )

                    if error_code not in RETRYABLE_ERROR_CODES or attempt >= max_retries:
                        step.status = StepStatus.FAILED
                        step.error = _sanitize_error(str(exc))
                        step.error_code = error_code
                        step.completed_at = utc_now()
                        self._fire_progress(step, config, "failed", start_time)
                        logger.warning("Step failed: %s (code=%s)", step.error, step.error_code)
                        span.retries = step.retries
                        return step

                    step.retries += 1
                    backoff = jittered_backoff(attempt)
                    logger.info(
                        "Retry %d/%d after %s (backoff=%.1fs)",
                        attempt + 1,
                        max_retries,
                        error_code,
                        backoff,
                    )

                    elapsed = time.monotonic() - start_time
                    if elapsed + backoff >= timeout:
                        step.status = StepStatus.FAILED
                        step.error = _sanitize_error(str(exc))
                        step.error_code = error_code
                        step.completed_at = utc_now()
                        self._fire_progress(step, config, "failed", start_time)
                        logger.warning("Retry aborted: global timeout would be exceeded")
                        span.retries = step.retries
                        return step

                    await asyncio.sleep(backoff)

        return step  # pragma: no cover


class SyncProvider(BaseProvider):
    """Base for providers with synchronous APIs (OpenAI, Stability, ElevenLabs).

    Subclasses implement a single ``generate()`` method instead of the
    three-method submit/poll/fetch_output lifecycle. The base class wraps
    ``generate()`` into the lifecycle automatically.

    Thread-safe: results are keyed by step_id, so concurrent invocations
    don't interfere with each other.

    Example::

        class OpenAIProvider(SyncProvider):
            name = "openai"

            def generate(self, step, config=None):
                resp = openai.images.generate(prompt=step.prompt, **step.params)
                for url in resp.data:
                    validate_asset_url(url.url)
                    step.assets.append(Asset(url=url.url, media_type="image/png"))
                return step
    """

    def __init__(self, *, models: ModelRegistry | None = None) -> None:
        super().__init__(models=models)
        # Results keyed by step_id — avoids monkey-patching Pydantic models
        self._sync_results: dict[str, Step] = {}

    @abstractmethod
    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Execute generation synchronously and return step with populated assets."""
        ...

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Calls generate() and stashes the result for fetch_output."""
        # Clear any stale result from a prior failed attempt (retry safety)
        self._sync_results.pop(step.step_id, None)
        result = self.generate(step, config)
        self._sync_results[step.step_id] = result
        return "sync"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        return self._sync_results.pop(step.step_id, step)
