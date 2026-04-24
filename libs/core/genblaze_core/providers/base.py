"""Base provider — abstract interface for media generation APIs."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from genblaze_core._utils import _SECRET_PATTERNS, jittered_backoff, utc_now
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import (
    RETRYABLE_ERROR_CODES,
    Modality,
    ProviderErrorCode,
    StepStatus,
)
from genblaze_core.models.step import Step
from genblaze_core.observability.span import StepSpan
from genblaze_core.providers.model_registry import EMPTY_REGISTRY, ModelRegistry, compute_cost
from genblaze_core.providers.progress import ProgressEvent
from genblaze_core.runnable.base import Runnable
from genblaze_core.runnable.config import RunnableConfig

logger = logging.getLogger("genblaze.provider")

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
    # Max consecutive transient poll failures tolerated before escalating.
    # Counter resets on any successful poll, so this is a consecutive-failure
    # budget, not a total. Set to 0 to disable intra-poll retries.
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
        self._models: ModelRegistry = models or type(self).models_default()

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

    def _attempt_once(
        self, step: Step, config: RunnableConfig | None, timeout: float, start_time: float
    ) -> Step:
        """Execute a single submit→poll→fetch attempt with adaptive polling."""
        self._cleanup_poll_cache()
        step.started_at = utc_now()
        step.status = StepStatus.SUBMITTED
        self._fire_progress(step, config, "submitted", start_time)

        logger.debug("Submitting to %s: model=%s", self.name, step.model)
        raw = self.submit(step, config)

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

        transient_retries = 0
        while True:
            try:
                done = self.poll(prediction_id, config)
            except Exception as exc:
                code = self._classify_poll_exc(exc)
                elapsed = time.monotonic() - start_time
                if (
                    code in RETRYABLE_ERROR_CODES
                    and transient_retries < self.poll_transient_retries
                    and elapsed < timeout
                ):
                    transient_retries += 1
                    backoff = jittered_backoff(transient_retries)
                    logger.debug(
                        "Poll transient error (%s), retry %d/%d in %.1fs",
                        code,
                        transient_retries,
                        self.poll_transient_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise
            transient_retries = 0  # reset on any successful poll
            if done:
                break
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise ProviderError(f"Poll timeout after {elapsed:.1f}s (limit: {timeout}s)")
            self._fire_progress(step, config, "processing", start_time)
            interval = _adaptive_poll_interval(elapsed, self.poll_interval)
            time.sleep(interval)

        step = self.fetch_output(prediction_id, step)
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
            transient_retries = 0
            while True:
                try:
                    done = self.poll(prediction_id, config)
                except Exception as exc:
                    code = self._classify_poll_exc(exc)
                    elapsed = time.monotonic() - start_time
                    if (
                        code in RETRYABLE_ERROR_CODES
                        and transient_retries < self.poll_transient_retries
                        and elapsed < timeout
                    ):
                        transient_retries += 1
                        time.sleep(jittered_backoff(transient_retries))
                        continue
                    raise
                transient_retries = 0
                if done:
                    break
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    msg = f"Resume poll timeout after {elapsed:.1f}s (limit: {timeout}s)"
                    raise ProviderError(msg)
                self._fire_progress(step, config, "processing", start_time)
                interval = _adaptive_poll_interval(elapsed, self.poll_interval)
                time.sleep(interval)

            step = self.fetch_output(prediction_id, step)
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
            transient_retries = 0
            while True:
                try:
                    done = await asyncio.to_thread(self.poll, prediction_id, config)
                except Exception as exc:
                    code = self._classify_poll_exc(exc)
                    elapsed = time.monotonic() - start_time
                    if (
                        code in RETRYABLE_ERROR_CODES
                        and transient_retries < self.poll_transient_retries
                        and elapsed < timeout
                    ):
                        transient_retries += 1
                        await asyncio.sleep(jittered_backoff(transient_retries))
                        continue
                    raise
                transient_retries = 0
                if done:
                    break
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    msg = f"Resume poll timeout after {elapsed:.1f}s (limit: {timeout}s)"
                    raise ProviderError(msg)
                self._fire_progress(step, config, "processing", start_time)
                interval = _adaptive_poll_interval(elapsed, self.poll_interval)
                await asyncio.sleep(interval)

            step = await asyncio.to_thread(self.fetch_output, prediction_id, step)
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
        step.started_at = utc_now()
        step.status = StepStatus.SUBMITTED
        self._fire_progress(step, config, "submitted", start_time)

        logger.debug("Submitting to %s: model=%s", self.name, step.model)
        raw = await asyncio.to_thread(self.submit, step, config)

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

        transient_retries = 0
        while True:
            try:
                done = await asyncio.to_thread(self.poll, prediction_id, config)
            except Exception as exc:
                code = self._classify_poll_exc(exc)
                elapsed = time.monotonic() - start_time
                if (
                    code in RETRYABLE_ERROR_CODES
                    and transient_retries < self.poll_transient_retries
                    and elapsed < timeout
                ):
                    transient_retries += 1
                    backoff = jittered_backoff(transient_retries)
                    logger.debug(
                        "Poll transient error (%s), retry %d/%d in %.1fs",
                        code,
                        transient_retries,
                        self.poll_transient_retries,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise
            transient_retries = 0
            if done:
                break
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise ProviderError(f"Poll timeout after {elapsed:.1f}s (limit: {timeout}s)")
            self._fire_progress(step, config, "processing", start_time)
            interval = _adaptive_poll_interval(elapsed, self.poll_interval)
            await asyncio.sleep(interval)

        step = await asyncio.to_thread(self.fetch_output, prediction_id, step)
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
