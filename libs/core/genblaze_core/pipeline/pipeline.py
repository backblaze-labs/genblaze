"""Pipeline — high-level fluent API for multi-step generation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from genblaze_core._utils import _SECRET_PATTERNS, new_id, utc_now
from genblaze_core.builders.run_builder import RunBuilder
from genblaze_core.exceptions import (
    BatchPipelineError,
    GenblazeError,
    PipelineError,
    PipelineTimeoutError,
)
from genblaze_core.models.enums import (
    Modality,
    ProviderErrorCode,
    RunStatus,
    StepStatus,
    StepType,
)
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.prompt_template import PromptTemplate
from genblaze_core.models.step import Step
from genblaze_core.observability.events import (
    PipelineCompletedEvent,
    PipelineFailedEvent,
    PipelineStartedEvent,
    StepQueuedEvent,
    StepStartedEvent,
    StreamEvent,
)
from genblaze_core.observability.tracer import LoggingTracer, NoOpTracer, Tracer, safe_call
from genblaze_core.pipeline.moderation import ModerationHook, ModerationResult
from genblaze_core.pipeline.result import PipelineResult, StepCompleteEvent
from genblaze_core.pipeline.streaming import QueueEmitter, progress_to_stream_event
from genblaze_core.progress_display import Spinner, should_auto_enable
from genblaze_core.providers.base import BaseProvider
from genblaze_core.runnable.base import Runnable
from genblaze_core.runnable.config import RunnableConfig

logger = logging.getLogger("genblaze.pipeline")


# Sentinel for ``raise_on_failure``. ``None`` means "caller didn't pass it,
# warn about the upcoming default flip"; ``True`` / ``False`` are explicit.
# In genblaze-core 0.4.0 the default becomes ``True`` and the sentinel is
# removed.
_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION = "0.4.0"


def _resolve_raise_on_failure(
    raise_on_failure: bool | None,
    *,
    exception_type: str = "PipelineError",
    surface: str = "Pipeline.run()",
) -> bool:
    """Resolve the ``raise_on_failure`` arg, emitting one ``DeprecationWarning``.

    Today's silent contract (``run()`` returns a failed ``PipelineResult``) is
    a footgun — pipelines stamp manifests over failed runs without notifying
    the caller. The fix is to raise; the deprecation cycle keeps existing
    code working until 0.4.0 with a single visible warning per call site.

    ``exception_type`` and ``surface`` parameterize the warning so batch and
    single-pipeline call sites mention the right class
    (``BatchPipelineError`` vs ``PipelineError``).
    """
    if raise_on_failure is not None:
        return raise_on_failure
    import warnings

    warnings.warn(
        f"{surface} will raise {exception_type} on step failure starting in "
        f"genblaze-core {_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION}. To opt in "
        "today, pass raise_on_failure=True. To preserve the current behavior, "
        "pass raise_on_failure=False.",
        DeprecationWarning,
        stacklevel=3,
    )
    return False


def _maybe_raise_pipeline_error(
    pipeline_result: PipelineResult,
    completed_steps: list[Step],
    raise_on_failure: bool,
) -> None:
    """If ``raise_on_failure`` and any step failed, raise ``PipelineError``."""
    if not raise_on_failure:
        return
    if pipeline_result.run.status != RunStatus.FAILED:
        return
    failed_idx: int | None = None
    failed_err: str | None = None
    for i, s in enumerate(completed_steps):
        if s.status == StepStatus.FAILED:
            failed_idx = i
            failed_err = s.error
            break
    raise PipelineError(
        f"Pipeline {pipeline_result.run.run_id} failed at step {failed_idx}: {failed_err}",
        result=pipeline_result,
        failed_step_index=failed_idx,
        failed_step_error=failed_err,
    )


def _maybe_raise_batch_error(
    results: list[PipelineResult],
    raise_on_failure: bool,
) -> None:
    """If ``raise_on_failure`` and any batch item failed, raise ``BatchPipelineError``.

    Called by ``batch_run`` / ``abatch_run`` **after** every item has run, so
    the caller can salvage successes via ``exc.succeeded`` even when some
    items failed. The early-return guards keep the happy path branch-free.
    """
    if not raise_on_failure:
        return
    if not any(r.run.status == RunStatus.FAILED for r in results):
        return
    raise BatchPipelineError(results)


def _reject_credentials_in_params(params: dict[str, Any], provider_name: str, model: str) -> None:
    """Raise GenblazeError if any string value in params looks like an API token.

    Walks nested dicts/lists. step.params lands in canonical_hash, embedded
    media, and persisted manifests — credential material here leaks forever.
    """

    def _scan(value: Any, path: str) -> None:
        # Accept bytes/bytearray as well — otherwise a caller could slip a
        # token past this guard by passing the UTF-8 bytes of the token
        # instead of the string, and still have it serialized downstream.
        if isinstance(value, (str, bytes, bytearray)):
            text = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
            if _SECRET_PATTERNS.search(text):
                raise GenblazeError(
                    f"step.params[{path}] for {provider_name}/{model} looks "
                    "like an API credential. step.params is hashed, embedded "
                    "into media, and persisted — never put secrets here. "
                    "Pass credentials via the provider constructor or "
                    "environment variables instead."
                )
        elif isinstance(value, dict):
            for k, v in value.items():
                _scan(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                _scan(v, f"{path}[{i}]")

    for k, v in params.items():
        _scan(v, str(k))


if TYPE_CHECKING:
    from genblaze_core.models.asset import Asset
    from genblaze_core.pipeline.cache import StepCache
    from genblaze_core.pipeline.template import PipelineTemplate
    from genblaze_core.sinks.base import BaseSink


@dataclass
class _PipelineStep:
    """A deferred step in a pipeline."""

    provider: BaseProvider
    model: str
    prompt: str | PromptTemplate | None
    params: dict
    modality: Modality
    step_type: StepType
    fallback_models: list[str] = field(default_factory=list)
    input_from: list[int] | None = None
    # Caller-supplied ETA hint surfaced on StepStartedEvent so consumers can
    # render meaningful progress UIs without hard-coding per-model duration.
    expected_duration_sec: float | None = None


@dataclass(frozen=True)
class _StepContext:
    """Context passed through step execution — keeps run/step/tracer metadata
    in one object instead of plumbing three kwargs through every call site."""

    run_id: str
    step_index: int
    total_steps: int


class Pipeline(Runnable[None, PipelineResult]):
    """Fluent API for constructing and executing multi-step generation pipelines.

    Usage:
        result = (
            Pipeline("my-pipeline")
            .step(replicate, model="flux-schnell", prompt="a cat")
            .step(replicate, model="flux-pro", prompt="enhance")
            .run()
        )

    Args:
        name: Human-readable pipeline name.
        tenant_id: Optional tenant ID for multi-tenant deployments.
        chain: If True, output assets from each step are passed as inputs
            to the next step (sequential dependency). Default False.
        structured_log: If True, emit JSON log events via StructuredLogger.
    """

    def __init__(
        self,
        name: str | None = None,
        tenant_id: str | None = None,
        *,
        project_id: str | None = None,
        chain: bool = False,
        structured_log: bool = False,
        max_concurrency: int | None = None,
        moderation: ModerationHook | None = None,
        tracer: Tracer | None = None,
    ):
        self._name = name
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._parent_run_id: str | None = None
        self._steps: list[_PipelineStep] = []
        self._config: RunnableConfig | None = None
        self._cache: StepCache | None = None
        self._chain = chain
        if max_concurrency is not None and max_concurrency < 1:
            raise GenblazeError(
                f"max_concurrency must be None (unlimited) or >= 1, got {max_concurrency}"
            )
        self._max_concurrency = max_concurrency
        self._moderation = moderation
        # Tracer resolution: explicit arg wins; legacy structured_log=True maps
        # to LoggingTracer so existing callers keep their JSON event stream.
        if tracer is not None:
            self._tracer: Tracer = tracer
        elif structured_log:
            self._tracer = LoggingTracer()
        else:
            self._tracer = NoOpTracer()
        self._event_emitter: QueueEmitter | None = None

    def config(self, cfg: RunnableConfig) -> Pipeline:
        self._config = cfg
        return self

    def tracer(self, tracer: Tracer) -> Pipeline:
        """Attach a tracer for run/step/event observability."""
        self._tracer = tracer
        return self

    def cache(self, cache: StepCache) -> Pipeline:
        """Enable step-level caching with the given cache instance."""
        self._cache = cache
        return self

    def from_result(self, result: PipelineResult) -> Pipeline:
        """Link this pipeline to a previous result for iteration lineage.

        Sets parent_run_id on the resulting run so manifests carry a pointer
        to the previous iteration. Does not affect the canonical hash.
        """
        self._parent_run_id = result.run.run_id
        return self

    def estimated_cost(self) -> Decimal | None:
        """Sum the upfront USD estimate across every configured step.

        Each step's contribution comes from
        :meth:`BaseProvider.estimate_cost` using the step's params (so
        per-second video pricing reads ``duration`` correctly). Returns
        ``None`` if any step is non-estimable (unknown model, response-only
        pricing, no pricing strategy at all) — apps should display "varies"
        rather than a misleading partial total.
        """
        total = Decimal("0")
        for ps in self._steps:
            cost = ps.provider.estimate_cost(ps.model, ps.params)
            if cost is None:
                return None
            total += cost
        return total

    def step(
        self,
        provider: BaseProvider,
        *,
        model: str,
        prompt: str | PromptTemplate | None = None,
        modality: Modality = Modality.IMAGE,
        step_type: StepType = StepType.GENERATE,
        fallback_models: list[str] | None = None,
        input_from: list[int] | int | None = None,
        expected_duration_sec: float | None = None,
        **params,
    ) -> Pipeline:
        """Add a step to the pipeline.

        Args:
            expected_duration_sec: Caller-supplied ETA hint (seconds), echoed
                on the ``step.started`` event so consumers can render progress
                UIs. The SDK does not synthesize this — supply your own median
                from observed runs. Stale values produce worse UX than
                omitting the field; treat as informational.
        """
        # Normalize scalar index to list for uniform handling
        normalized_from: list[int] | None = None
        if input_from is not None:
            normalized_from = [input_from] if isinstance(input_from, int) else list(input_from)
        self._steps.append(
            _PipelineStep(
                provider=provider,
                model=model,
                prompt=prompt,
                params=params,
                modality=modality,
                step_type=step_type,
                fallback_models=fallback_models or [],
                input_from=normalized_from,
                expected_duration_sec=expected_duration_sec,
            )
        )
        return self

    def _validate_steps(self) -> None:
        """Validate step capabilities before execution. Fails loud on mismatches."""
        for i, ps in enumerate(self._steps):
            caps = ps.provider.get_capabilities()
            if caps is None:
                continue

            # Check modality support
            if caps.supported_modalities and ps.modality not in caps.supported_modalities:
                supported = ", ".join(str(m) for m in caps.supported_modalities)
                msg = (
                    f"Step {i} ({ps.provider.name}): modality '{ps.modality}' not supported."
                    f" Supported: [{supported}]"
                )
                raise GenblazeError(msg)

            # In chain mode, downstream steps must accept chain inputs
            receives_chain = (self._chain and i > 0) or ps.input_from is not None
            if receives_chain and not caps.accepts_chain_input:
                msg = (
                    f"Step {i} ({ps.provider.name}): receives chained inputs but provider"
                    f" does not accept chain input. Set accepts_chain_input=True in"
                    f" ProviderCapabilities or remove this step from the chain."
                )
                raise GenblazeError(msg)

    def _resolve_inputs(
        self,
        ps: _PipelineStep,
        step_index: int,
        completed_steps: list[Step],
        prev_assets: list[Asset],
    ) -> list[Asset] | None:
        """Determine inputs for a step based on input_from, chain mode, or neither."""
        if ps.input_from is not None:
            for idx in ps.input_from:
                if idx < 0 or idx >= step_index:
                    msg = (
                        f"input_from index {idx} is out of range for step {step_index}"
                        f" (only {step_index} prior steps completed)"
                    )
                    raise GenblazeError(msg)
            assets: list[Asset] = []
            for idx in ps.input_from:
                assets.extend(completed_steps[idx].assets)
            return assets
        if self._chain:
            return prev_assets
        return None

    def _build_step(
        self,
        ps: _PipelineStep,
        inputs: list[Asset] | None = None,
        *,
        step_id: str | None = None,
    ) -> Step:
        """Create a Step model from a deferred pipeline step.

        Normalizes params via the provider's normalize_params() so cache keys
        and manifests use consistent parameter names. Extracts seed and
        negative_prompt from params into their top-level Step fields.

        Args:
            step_id: Optional pre-allocated UUID. Lets the caller emit
                ``step.queued`` events referencing the same id this step
                will carry once built. Default: a new UUID.
        """
        # Guard: PromptTemplate must be rendered before execution
        if isinstance(ps.prompt, PromptTemplate):
            msg = (
                "Step prompt is a PromptTemplate but was not rendered. "
                "Use batch_run() with dicts or call template.render() "
                "before passing to step()."
            )
            raise GenblazeError(msg)

        normalized = ps.provider.normalize_params(ps.params, ps.modality)
        # Reject credential-shaped values up front. step.params is provenance:
        # it is hashed, embedded into media, and persisted to manifests/parquet.
        # If a token slips in here, it leaks permanently. Pass credentials via
        # the provider constructor or environment variables instead.
        _reject_credentials_in_params(normalized, ps.provider.name, ps.model)
        # Extract Step-level fields that callers pass via **params
        seed = normalized.pop("seed", None)
        negative_prompt = normalized.pop("negative_prompt", None)

        # Persist pipeline graph info in metadata for faithful replay
        metadata: dict[str, Any] = {}
        if ps.fallback_models:
            metadata["_fallback_models"] = ps.fallback_models
        if ps.input_from is not None:
            metadata["_input_from"] = ps.input_from

        step_kwargs: dict[str, Any] = dict(
            provider=ps.provider.name,
            model=ps.model,
            prompt=ps.prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            params=normalized,
            modality=ps.modality,
            step_type=ps.step_type,
            status=StepStatus.PENDING,
            inputs=inputs or [],
            metadata=metadata,
        )
        if step_id is not None:
            step_kwargs["step_id"] = step_id
        return Step(**step_kwargs)

    def _apply_moderation_failure(
        self,
        step: Step,
        mod_result: ModerationResult,
        stage: str,
    ) -> Step:
        """Mark a step as failed due to moderation rejection."""
        label = "prompt" if stage == "pre" else "output"
        step.status = StepStatus.FAILED
        step.error = f"Moderation rejected {label}: {mod_result.reason or 'no reason given'}"
        step.error_code = ProviderErrorCode.INVALID_INPUT
        step.metadata["moderation"] = {
            "stage": stage,
            "reason": mod_result.reason,
            "flagged_categories": mod_result.flagged_categories,
        }
        return step

    def _try_fallback_models(
        self,
        ps: _PipelineStep,
        step: Step,
        result: Step,
        config: RunnableConfig | None,
        invoke_fn: Any,
    ) -> tuple[Step, Step]:
        """Try fallback models on MODEL_ERROR. Returns (result, cache_key_step).

        Used by the sync _execute_step path. The async path inlines its own
        version because ainvoke requires await.
        """
        cache_key_step = step
        if (
            result.status == StepStatus.FAILED
            and result.error_code == ProviderErrorCode.MODEL_ERROR
            and ps.fallback_models
        ):
            original_model = step.model
            for fb_model in ps.fallback_models:
                logger.info("Falling back from %s to %s", step.model, fb_model)
                fb_step = self._build_step(ps, step.inputs or None)
                fb_step.model = fb_model
                fb_step.metadata = {
                    "fallback_from": original_model,
                    "fallback_model": fb_model,
                }
                result = invoke_fn(fb_step, config)
                if result.status == StepStatus.SUCCEEDED:
                    cache_key_step = fb_step
                    break
        return result, cache_key_step

    def _post_step(
        self,
        ps: _PipelineStep,
        step: Step,
        result: Step,
        cache_key_step: Step,
        duration_ms: float,
        mod_result_fn,
    ) -> Step:
        """Post-step moderation, logging, and caching — shared by sync/async paths.

        mod_result_fn checks output assets; pass None to skip moderation.
        """
        if mod_result_fn is not None and result.status == StepStatus.SUCCEEDED and result.assets:
            try:
                mod_result = mod_result_fn(result.assets)
            except Exception as exc:
                result.status = StepStatus.FAILED
                result.error = f"Moderation hook error: {exc}"
                result.error_code = ProviderErrorCode.UNKNOWN
                return result
            if not mod_result.allowed:
                return self._apply_moderation_failure(result, mod_result, "post")

        if self._cache is not None and result.status == StepStatus.SUCCEEDED:
            self._cache.put(cache_key_step, result)

        return result

    def _execute_step(
        self,
        ps: _PipelineStep,
        step: Step,
        config: RunnableConfig | None,
        ctx: _StepContext,
    ) -> Step:
        """Execute a single step with moderation, caching, and fallback models."""
        if self._moderation is not None and step.prompt is not None:
            try:
                mod_result = self._moderation.check_prompt(step.prompt, step.params)
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = f"Moderation hook error: {exc}"
                step.error_code = ProviderErrorCode.UNKNOWN
                return step
            if not mod_result.allowed:
                return self._apply_moderation_failure(step, mod_result, "pre")

        if self._cache is not None:
            cached = self._cache.get(step)
            if cached is not None:
                return cached

        safe_call(
            self._tracer,
            "on_step_start",
            ctx.run_id,
            step,
            step_index=ctx.step_index,
            total_steps=ctx.total_steps,
        )
        t0 = time.monotonic()
        final: Step = step  # fallback for on_step_end if provider.invoke raises
        try:
            result = ps.provider.invoke(step, config)
            result, cache_key_step = self._try_fallback_models(
                ps, step, result, config, ps.provider.invoke
            )
            final = self._post_step(
                ps,
                step,
                result,
                cache_key_step,
                (time.monotonic() - t0) * 1000,
                self._moderation.check_output if self._moderation else None,
            )
            return final
        finally:
            safe_call(
                self._tracer,
                "on_step_end",
                ctx.run_id,
                final,
                duration_ms=(time.monotonic() - t0) * 1000,
                step_index=ctx.step_index,
            )

    async def _execute_step_async(
        self,
        ps: _PipelineStep,
        step: Step,
        config: RunnableConfig | None,
        ctx: _StepContext,
    ) -> Step:
        """Execute a single step asynchronously with moderation, caching, and fallback."""
        if self._moderation is not None and step.prompt is not None:
            try:
                mod_result = await self._moderation.acheck_prompt(step.prompt, step.params)
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = f"Moderation hook error: {exc}"
                step.error_code = ProviderErrorCode.UNKNOWN
                return step
            if not mod_result.allowed:
                return self._apply_moderation_failure(step, mod_result, "pre")

        if self._cache is not None:
            cached = self._cache.get(step)
            if cached is not None:
                return cached

        safe_call(
            self._tracer,
            "on_step_start",
            ctx.run_id,
            step,
            step_index=ctx.step_index,
            total_steps=ctx.total_steps,
        )
        t0 = time.monotonic()
        final: Step = step  # fallback for on_step_end if ainvoke raises
        try:
            result = await ps.provider.ainvoke(step, config)

            # Fallback loop (inlined because ainvoke requires await)
            cache_key_step = step
            if (
                result.status == StepStatus.FAILED
                and result.error_code == ProviderErrorCode.MODEL_ERROR
                and ps.fallback_models
            ):
                original_model = step.model
                for fb_model in ps.fallback_models:
                    logger.info("Falling back from %s to %s", step.model, fb_model)
                    fb_step = self._build_step(ps, step.inputs or None)
                    fb_step.model = fb_model
                    fb_step.metadata = {
                        "fallback_from": original_model,
                        "fallback_model": fb_model,
                    }
                    result = await ps.provider.ainvoke(fb_step, config)
                    if result.status == StepStatus.SUCCEEDED:
                        cache_key_step = fb_step
                        break

            # Async post-moderation — run before shared _post_step
            if (
                self._moderation is not None
                and result.status == StepStatus.SUCCEEDED
                and result.assets
            ):
                try:
                    mod_result = await self._moderation.acheck_output(result.assets)
                except Exception as exc:
                    result.status = StepStatus.FAILED
                    result.error = f"Moderation hook error: {exc}"
                    result.error_code = ProviderErrorCode.UNKNOWN
                    final = result
                    return result
                if not mod_result.allowed:
                    final = self._apply_moderation_failure(result, mod_result, "post")
                    return final

            final = self._post_step(
                ps,
                step,
                result,
                cache_key_step,
                (time.monotonic() - t0) * 1000,
                None,  # async moderation already handled above
            )
            return final
        finally:
            safe_call(
                self._tracer,
                "on_step_end",
                ctx.run_id,
                final,
                duration_ms=(time.monotonic() - t0) * 1000,
                step_index=ctx.step_index,
            )

    def _finalize(
        self,
        completed_steps: list[Step],
        sink: BaseSink | None,
        run_id: str,
        started_at_ts: float | None = None,
    ) -> PipelineResult:
        """Build run, manifest, and write to sink."""
        builder = RunBuilder(self._name)
        builder.run_id(run_id)
        if self._tenant_id:
            builder.tenant(self._tenant_id)
        if self._project_id:
            builder.project(self._project_id)
        if self._parent_run_id:
            builder.parent(self._parent_run_id)

        all_succeeded = all(s.status == StepStatus.SUCCEEDED for s in completed_steps)
        for s in completed_steps:
            builder.add_step(s)

        builder.status(RunStatus.COMPLETED if all_succeeded else RunStatus.FAILED)
        run_obj = builder.build()

        if started_at_ts is not None:
            run_obj.started_at = datetime.fromtimestamp(started_at_ts, tz=UTC)
        run_obj.completed_at = utc_now()

        manifest = Manifest.from_run(run_obj)

        if sink is not None:
            # sink.write_run() recomputes the hash after asset transfer mutations
            sink.write_run(run_obj, manifest)

        return PipelineResult(run_obj, manifest)

    # ------------------------------------------------------------------
    # Event / tracer plumbing — wired into run()/arun() and stream()/astream().
    # Internal helpers. Kept private so they can evolve without breaking callers.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Event emission — internal helpers feeding tracer + stream emitter.
    # ------------------------------------------------------------------

    @staticmethod
    def _call_user_callback(callback: Any, event: Any, name: str) -> None:
        """Invoke a user-supplied callback; swallow + log exceptions.

        User callbacks (on_progress, on_step_complete) live on the pipeline's
        critical path. If they raise, we'd lose the manifest write, partial
        assets, and tracer end-of-run events. Match the safety posture of
        moderation hooks and sink.on_step_complete.
        """
        if callback is None:
            return
        try:
            callback(event)
        except Exception:  # noqa: BLE001 — user code; log and keep going
            logger.warning("%s callback raised", name, exc_info=True)

    def attach_emitter(self, emitter: QueueEmitter | None) -> QueueEmitter | None:
        """Install (or clear) the stream event emitter, returning the prior one.

        Public so composable runners (e.g. AgentLoop) can pipe pipeline events
        into their own event stream without poking private state.
        """
        prior = self._event_emitter
        self._event_emitter = emitter
        return prior

    def _install_progress_tracer(
        self,
        config: RunnableConfig | None,
        user_on_progress: Any,
        run_id: str,
    ) -> RunnableConfig | None:
        """Wrap ``on_progress`` and ``on_retry`` so tracer + stream emitter see each tick.

        ``on_retry`` originates from ``BaseProvider._emit_retry`` and carries an
        already-constructed :class:`StepRetriedEvent`; we forward it verbatim
        to the stream so dashboards can surface retry attempts in real time.
        Without this wiring, retry events fire only into the user-supplied
        ``config["on_retry"]`` callback (if any) and never reach
        ``Pipeline.stream()`` consumers.
        """
        user_on_retry = (config or {}).get("on_retry")
        has_tracer = not isinstance(self._tracer, NoOpTracer)
        needs_install = (
            user_on_progress is not None
            or user_on_retry is not None
            or has_tracer
            or self._event_emitter is not None
        )
        if not needs_install:
            return config

        def _composite_progress(ev: Any) -> None:
            # User callback first so their side effects see the raw ProgressEvent
            # ordering; emit event to tracer/stream regardless of callback outcome.
            self._call_user_callback(user_on_progress, ev, "on_progress")
            self._emit_event(progress_to_stream_event(ev, run_id))

        def _composite_retry(ev: Any) -> None:
            # ``ev`` is a fully-formed StepRetriedEvent — emit verbatim.
            self._call_user_callback(user_on_retry, ev, "on_retry")
            self._emit_event(ev)

        merged: RunnableConfig = RunnableConfig(**config) if config else RunnableConfig()
        merged["on_progress"] = _composite_progress
        merged["on_retry"] = _composite_retry
        return merged

    def _emit_event(self, event: StreamEvent) -> None:
        safe_call(self._tracer, "on_event", event)
        if self._event_emitter is not None:
            self._event_emitter.put(event)

    def _emit_run_start(self, run_id: str, total_steps: int) -> None:
        safe_call(
            self._tracer,
            "on_run_start",
            run_id,
            self._name,
            tenant_id=self._tenant_id,
            total_steps=total_steps,
        )
        self._emit_event(
            PipelineStartedEvent(
                run_id=run_id,
                total_steps=total_steps,
                message=self._name,
            )
        )

    def _emit_step_start(self, ctx: _StepContext, step: Step, ps: _PipelineStep) -> None:
        self._emit_event(
            StepStartedEvent(
                run_id=ctx.run_id,
                step_id=step.step_id,
                step_index=ctx.step_index,
                total_steps=ctx.total_steps,
                provider=ps.provider.name,
                model=ps.model,
                expected_duration_sec=ps.expected_duration_sec,
            )
        )

    def _emit_step_queued(
        self,
        run_id: str,
        step: Step,
        ps: _PipelineStep,
        step_index: int,
        total_steps: int,
        reason: Literal["serial", "concurrency_limit"],
    ) -> None:
        """Emit a step.queued event. Additive — does not replace step.started."""
        self._emit_event(
            StepQueuedEvent(
                run_id=run_id,
                step_id=step.step_id,
                step_index=step_index,
                total_steps=total_steps,
                provider=ps.provider.name,
                model=ps.model,
                reason=reason,
            )
        )

    def _emit_step_complete_event(self, step_event: StepCompleteEvent, run_id: str) -> None:
        # Tracer already gets on_step_end inside _execute_step; emitter-only here.
        if self._event_emitter is not None:
            self._event_emitter.on_step_complete(step_event)

    def _notify_sink_step_complete(
        self,
        sink: BaseSink | None,
        step: Step,
        run_id: str,
        started_at_ts: float,
    ) -> None:
        """Fire the sink's on_step_complete hook if a sink is attached.

        Sinks that support eager asset transfer use this to kick off
        uploads while subsequent steps continue generating. Default
        implementation in BaseSink is a no-op; we still check
        ``hasattr`` to be kind to sinks that predate this hook.

        Failures here don't fail the pipeline — worst case, the sink's
        ``write_run`` will handle all transfers at the end.
        """
        if sink is None or not hasattr(sink, "on_step_complete"):
            return
        try:
            sink.on_step_complete(
                step,
                run_id=run_id,
                tenant_id=self._tenant_id,
                date_str=datetime.fromtimestamp(started_at_ts, tz=UTC).strftime("%Y-%m-%d"),
            )
        except Exception as exc:
            logger.warning(
                "sink.on_step_complete raised for step %s: %s — "
                "write_run will handle this asset's transfer instead.",
                step.step_id,
                exc,
            )

    def _emit_pipeline_end(self, result: PipelineResult, run_id: str) -> None:
        failed = result.run.status == RunStatus.FAILED
        # Pre-compute wire-safe fields so consumers of to_dict() / JSON Schema
        # still see terminal status + manifest hash when the in-process
        # PipelineResult is excluded from serialization.
        run_status = str(result.run.status)
        manifest_hash = result.manifest.canonical_hash
        if failed:
            event: StreamEvent = PipelineFailedEvent(
                run_id=run_id,
                result=result,
                message=result.error_summary(),
                run_status=run_status,
                manifest_hash=manifest_hash,
            )
        else:
            event = PipelineCompletedEvent(
                run_id=run_id,
                result=result,
                run_status=run_status,
                manifest_hash=manifest_hash,
            )
        self._emit_event(event)
        safe_call(self._tracer, "on_run_end", run_id, result)

    # ------------------------------------------------------------------
    # Streaming — sync and async iterators over StreamEvent.
    # ------------------------------------------------------------------

    def stream(self, *, heartbeats: bool = True, **run_kwargs: Any):
        """Run the pipeline in a worker thread and yield events as they occur.

        Emits ``pipeline.started``, ``step.started``, ``step.progress``,
        ``step.completed``/``step.failed``, then ``pipeline.completed``/
        ``pipeline.failed`` (with :class:`PipelineResult` attached).

        Args:
            heartbeats: When ``True`` (default), keepalive ``step.progress``
                events with ``is_heartbeat=True`` are emitted between
                long-poll intervals so SSE proxies and load balancers see
                an active connection. Set ``False`` for high-volume
                deployments where the keepalive overhead outweighs the
                benefit (heartbeat events are dropped at the emitter so
                they never reach the queue).

        Uncaught exceptions from the pipeline are re-raised after the
        event stream drains.

        Early break: if the caller breaks out of iteration before the
        terminal event, we return immediately and let the (daemon) worker
        thread finish in the background. Remaining events are discarded
        and any post-break exception in the pipeline is suppressed.
        """
        import queue as _queue
        import threading

        from genblaze_core.pipeline.streaming import drain_queue_sync

        q: _queue.Queue = _queue.Queue()
        emitter = QueueEmitter(q, include_heartbeats=heartbeats)
        prior = self.attach_emitter(emitter)

        exc_box: list[BaseException] = []
        done = threading.Event()

        def _worker() -> None:
            try:
                self.run(**run_kwargs)
            except BaseException as exc:  # noqa: BLE001 — propagate via queue
                exc_box.append(exc)
            finally:
                emitter.close()
                done.set()

        t = threading.Thread(target=_worker, daemon=True, name="genblaze-stream")
        t.start()
        try:
            yield from drain_queue_sync(q)
        finally:
            self.attach_emitter(prior)
            if done.is_set():
                t.join()  # fast — worker already returned
                if exc_box:
                    raise exc_box[0]
            # else: consumer broke early; worker keeps running as a daemon
            # thread and exits when the pipeline naturally completes.

    async def astream(self, *, heartbeats: bool = True, **run_kwargs: Any):
        """Async version of :meth:`stream`.

        Args:
            heartbeats: See :meth:`stream`. Default ``True``.

        Early break: cancels the worker task so in-flight provider calls
        unwind at their next await point. Any post-break exception from
        the pipeline is suppressed.
        """
        from genblaze_core.pipeline.streaming import drain_queue_async

        q: asyncio.Queue = asyncio.Queue()
        emitter = QueueEmitter(q, include_heartbeats=heartbeats)
        prior = self.attach_emitter(emitter)

        async def _worker() -> None:
            try:
                await self.arun(**run_kwargs)
            finally:
                emitter.close()

        task = asyncio.create_task(_worker())
        try:
            async for ev in drain_queue_async(q):
                yield ev
        finally:
            self.attach_emitter(prior)
            if task.done():
                await task  # re-raises if worker failed
            else:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                    logger.debug("astream worker aborted: %s", exc)

    def invoke(self, input: None = None, config: RunnableConfig | None = None) -> PipelineResult:
        """Runnable interface — delegates to run() with a local config copy."""
        effective_config = config if config is not None else self._config
        return self.run(_config_override=effective_config)

    async def ainvoke(
        self, input: None = None, config: RunnableConfig | None = None
    ) -> PipelineResult:
        """Async Runnable interface — delegates to arun() with a local config copy."""
        effective_config = config if config is not None else self._config
        return await self.arun(_config_override=effective_config)

    def run(
        self,
        *,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        raise_on_failure: bool | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        on_progress: Any = None,
        progress: bool | None = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
        on_retry: Any = None,
        _config_override: RunnableConfig | None = None,
    ) -> PipelineResult:
        """Execute all steps synchronously and return a PipelineResult.

        Args:
            sink: Optional sink to write run data to.
            fail_fast: If True (default), stop on first failed step.
            raise_on_failure: If ``True``, raise :class:`PipelineError` when any
                step ends in ``StepStatus.FAILED``. If ``False``, the failed
                ``PipelineResult`` is returned as today. ``None`` (default in
                0.3.x) emits a ``DeprecationWarning`` describing the 0.4.0
                default flip and behaves like ``False``.
            timeout: Per-step timeout in seconds (builds RunnableConfig internally).
            max_retries: Per-step max retries (builds RunnableConfig internally).
            on_progress: Optional callback fired during provider poll loops.
            progress: Show an interactive terminal spinner. ``None`` (default)
                auto-enables when stderr is a TTY and ``on_progress`` is unset;
                ``True`` forces on; ``False`` forces off. User-supplied
                ``on_progress`` always wins — no spinner is layered on top.
            pipeline_timeout: End-to-end timeout in seconds for the entire pipeline.
            on_step_complete: Optional callback fired after each step completes.
                Receives a StepCompleteEvent.
            on_retry: Optional callback fired before each retry sleep with a
                :class:`StepRetriedEvent`. Same event also flows through
                :meth:`stream` consumers — supply this only if you need a
                synchronous side-effect alongside (e.g., metrics).

        Raises:
            GenblazeError: If no steps have been added to the pipeline.
            GenblazeError: If pipeline_timeout is exceeded.
        """
        if not self._steps:
            msg = "Pipeline has no steps. Add steps with .step() before calling .run()."
            raise GenblazeError(msg)

        self._validate_steps()

        # Resolve config: explicit override > inline kwargs > pipeline-level config
        config: RunnableConfig | None
        if _config_override is not None:
            config = _config_override
        elif timeout is not None or max_retries is not None:
            config = RunnableConfig(
                timeout=timeout if timeout is not None else 30,
                max_retries=max_retries if max_retries is not None else 0,
            )
        else:
            config = self._config

        run_id = new_id()
        spinner: Spinner | None = None
        if should_auto_enable(on_progress, progress):
            spinner = Spinner()
            spinner.start()
            on_progress = spinner
        if on_retry is not None:
            # Splice a user-supplied callback into config so the progress tracer
            # composite picks it up alongside the stream emitter.
            config = RunnableConfig(**(config or {}))
            config["on_retry"] = on_retry
        config = self._install_progress_tracer(config, on_progress, run_id)

        started_at_ts = time.time()
        started_at_mono = time.monotonic()
        logger.info("Starting pipeline %r with %d steps", self._name, len(self._steps))

        total_steps = len(self._steps)
        self._emit_run_start(run_id, total_steps)
        # Pre-allocate step_ids so step.queued events can reference the same
        # id the step will carry once built. Sequential pipelines emit queued
        # events for every step except the first (which starts immediately).
        step_ids = [new_id() for _ in self._steps]
        for idx, ps in enumerate(self._steps[1:], start=1):
            self._emit_step_queued(
                run_id=run_id,
                step=Step(step_id=step_ids[idx], provider=ps.provider.name, model=ps.model),
                ps=ps,
                step_index=idx,
                total_steps=total_steps,
                reason="serial",
            )
        completed_steps: list[Step] = []
        prev_assets: list[Asset] = []
        pipeline_result: PipelineResult | None = None
        try:
            for i, ps in enumerate(self._steps, 1):
                # Check pipeline-level timeout before each step
                if pipeline_timeout is not None:
                    elapsed = time.monotonic() - started_at_mono
                    if elapsed >= pipeline_timeout:
                        msg = (
                            f"Pipeline timeout exceeded after {elapsed:.1f}s"
                            f" (limit: {pipeline_timeout}s)"
                        )
                        raise PipelineTimeoutError(msg)

                inputs = self._resolve_inputs(ps, i - 1, completed_steps, prev_assets)
                step = self._build_step(ps, inputs, step_id=step_ids[i - 1])
                logger.debug(
                    "Executing step %d/%d: %s/%s",
                    i,
                    len(self._steps),
                    ps.provider.name,
                    ps.model,
                )
                ctx = _StepContext(run_id=run_id, step_index=i - 1, total_steps=total_steps)
                self._emit_step_start(ctx, step, ps)
                if spinner is not None:
                    spinner.step_starting(
                        ps.provider.name,
                        ps.model,
                        prompt=step.prompt,
                        step_index=i - 1,
                        total=total_steps,
                    )
                result = self._execute_step(ps, step, config, ctx)
                if spinner is not None:
                    spinner.step_done(ok=result.status == StepStatus.SUCCEEDED)
                completed_steps.append(result)

                # Give the sink a chance to eagerly start asset transfers
                # while subsequent steps keep generating. Sinks without
                # eager support (default) no-op here.
                self._notify_sink_step_complete(sink, result, run_id, started_at_ts)

                step_event = StepCompleteEvent(
                    step_index=i - 1,
                    total_steps=total_steps,
                    step=result,
                    elapsed_sec=time.monotonic() - started_at_mono,
                )
                self._call_user_callback(on_step_complete, step_event, "on_step_complete")
                self._emit_step_complete_event(step_event, run_id)

                if result.status == StepStatus.SUCCEEDED:
                    if self._chain:
                        prev_assets = list(result.assets)
                elif result.status == StepStatus.FAILED:
                    if self._chain:
                        prev_assets = []
                    logger.warning("Step %d failed: %s", i, result.error)
                    if fail_fast:
                        break

            pipeline_result = self._finalize(completed_steps, sink, run_id, started_at_ts)
            logger.info(
                "Pipeline complete: status=%s, run_id=%s",
                pipeline_result.run.status,
                pipeline_result.run.run_id,
            )
            # Resolve raise_on_failure inside the try-block so the finally
            # still emits pipeline_end. PipelineError propagates after.
            should_raise = _resolve_raise_on_failure(raise_on_failure)
            _maybe_raise_pipeline_error(pipeline_result, completed_steps, should_raise)
            return pipeline_result
        finally:
            # Guarantee on_run_end fires — covers timeouts, KeyboardInterrupt,
            # and bugs in _finalize. Synthesizes an aborted result if the
            # normal flow didn't reach _finalize.
            self._emit_pipeline_end(
                pipeline_result or self._finalize(completed_steps, None, run_id, started_at_ts),
                run_id,
            )
            if spinner is not None:
                spinner.stop()

    async def arun(
        self,
        *,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        raise_on_failure: bool | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_concurrency: int | None = None,
        on_progress: Any = None,
        progress: bool | None = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
        on_retry: Any = None,
        _config_override: RunnableConfig | None = None,
    ) -> PipelineResult:
        """Execute steps asynchronously and return a PipelineResult.

        When chain=False, all steps run concurrently via asyncio.gather.
        When chain=True, steps run sequentially (each feeds the next).

        Args:
            sink: Optional sink to write run data to.
            fail_fast: If True (default), stop on first failed step.
            timeout: Per-step timeout in seconds (builds RunnableConfig internally).
            max_retries: Per-step max retries (builds RunnableConfig internally).
            on_progress: Optional callback fired during provider poll loops.
            progress: Show an interactive terminal spinner during sequential
                execution. ``None`` (default) auto-enables when stderr is a
                TTY and ``on_progress`` is unset. The spinner is skipped in
                concurrent mode (chain=False, no input_from) since multiple
                steps run in parallel and can't be represented on one line.
            pipeline_timeout: End-to-end timeout in seconds for the entire pipeline.
            on_step_complete: Optional callback fired after each step completes.
                Receives a StepCompleteEvent.
            on_retry: Optional callback fired before each retry sleep with a
                :class:`StepRetriedEvent`. Same event also flows through
                :meth:`astream` consumers.

        Raises:
            GenblazeError: If no steps have been added to the pipeline.
            GenblazeError: If pipeline_timeout is exceeded.
        """
        if not self._steps:
            msg = "Pipeline has no steps. Add steps with .step() before calling .arun()."
            raise GenblazeError(msg)

        # Validate max_concurrency up front so we never emit run_start / step_start
        # tracer events for a run that can't execute.
        if max_concurrency is not None and max_concurrency < 1:
            raise GenblazeError(
                f"max_concurrency must be None (unlimited) or >= 1, got {max_concurrency}"
            )

        self._validate_steps()

        # Resolve config: explicit override > inline kwargs > pipeline-level config
        config: RunnableConfig | None
        if _config_override is not None:
            config = _config_override
        elif timeout is not None or max_retries is not None:
            config = RunnableConfig(
                timeout=timeout if timeout is not None else 30,
                max_retries=max_retries if max_retries is not None else 0,
            )
        else:
            config = self._config

        run_id = new_id()
        # input_from requires sequential execution (needs prior step results)
        has_input_from = any(ps.input_from is not None for ps in self._steps)
        sequential = self._chain or has_input_from
        spinner: Spinner | None = None
        if sequential and should_auto_enable(on_progress, progress):
            spinner = Spinner()
            spinner.start()
            on_progress = spinner
        if on_retry is not None:
            config = RunnableConfig(**(config or {}))
            config["on_retry"] = on_retry
        config = self._install_progress_tracer(config, on_progress, run_id)

        started_at_ts = time.time()
        started_at_mono = time.monotonic()
        total_steps = len(self._steps)
        logger.info(
            "Starting async pipeline %r with %d steps",
            self._name,
            total_steps,
        )
        self._emit_run_start(run_id, total_steps)
        completed_steps: list[Step] = []
        pipeline_result: PipelineResult | None = None
        try:
            if sequential:
                # Sequential: each step's outputs feed the next step's inputs.
                # Pre-allocate step_ids so step.queued events match the ids
                # the steps will carry once built.
                step_ids = [new_id() for _ in self._steps]
                for idx, ps_q in enumerate(self._steps[1:], start=1):
                    self._emit_step_queued(
                        run_id=run_id,
                        step=Step(
                            step_id=step_ids[idx],
                            provider=ps_q.provider.name,
                            model=ps_q.model,
                        ),
                        ps=ps_q,
                        step_index=idx,
                        total_steps=total_steps,
                        reason="serial",
                    )
                prev_assets: list[Asset] = []
                for i, ps in enumerate(self._steps, 1):
                    if pipeline_timeout is not None:
                        elapsed = time.monotonic() - started_at_mono
                        if elapsed >= pipeline_timeout:
                            msg = (
                                f"Pipeline timeout exceeded after {elapsed:.1f}s"
                                f" (limit: {pipeline_timeout}s)"
                            )
                            raise PipelineTimeoutError(msg)

                    inputs = self._resolve_inputs(ps, i - 1, completed_steps, prev_assets)
                    step = self._build_step(ps, inputs, step_id=step_ids[i - 1])
                    logger.debug(
                        "Executing step %d/%d: %s/%s",
                        i,
                        len(self._steps),
                        ps.provider.name,
                        ps.model,
                    )
                    ctx = _StepContext(run_id=run_id, step_index=i - 1, total_steps=total_steps)
                    self._emit_step_start(ctx, step, ps)
                    if spinner is not None:
                        spinner.step_starting(
                            ps.provider.name,
                            ps.model,
                            prompt=step.prompt,
                            step_index=i - 1,
                            total=total_steps,
                        )
                    result = await self._execute_step_async(ps, step, config, ctx)
                    if spinner is not None:
                        spinner.step_done(ok=result.status == StepStatus.SUCCEEDED)
                    completed_steps.append(result)

                    # Eager-transfer hook (sinks opt in).
                    self._notify_sink_step_complete(sink, result, run_id, started_at_ts)

                    step_event = StepCompleteEvent(
                        step_index=i - 1,
                        total_steps=total_steps,
                        step=result,
                        elapsed_sec=time.monotonic() - started_at_mono,
                    )
                    self._call_user_callback(on_step_complete, step_event, "on_step_complete")
                    self._emit_step_complete_event(step_event, run_id)

                    if result.status == StepStatus.SUCCEEDED:
                        if self._chain:
                            prev_assets = list(result.assets)
                    elif result.status == StepStatus.FAILED:
                        if self._chain:
                            prev_assets = []
                        logger.warning("Step %d failed: %s", i, result.error)
                        if fail_fast:
                            break
            else:
                # Concurrent: use as_completed + cancel on failure when fail_fast
                steps_and_models = [(ps, self._build_step(ps)) for ps in self._steps]
                for idx, (ps, step) in enumerate(steps_and_models):
                    self._emit_step_start(
                        _StepContext(run_id=run_id, step_index=idx, total_steps=total_steps),
                        step,
                        ps,
                    )

                if pipeline_timeout is not None:
                    elapsed = time.monotonic() - started_at_mono
                    if elapsed >= pipeline_timeout:
                        msg = (
                            f"Pipeline timeout exceeded after {elapsed:.1f}s"
                            f" (limit: {pipeline_timeout}s)"
                        )
                        raise PipelineTimeoutError(msg)

                concurrency = max_concurrency or self._max_concurrency
                sem = asyncio.Semaphore(concurrency) if concurrency else None
                step_positions = {
                    step.step_id: idx for idx, (_, step) in enumerate(steps_and_models)
                }

                def _ctx_for(step: Step) -> _StepContext:
                    return _StepContext(
                        run_id=run_id,
                        step_index=step_positions[step.step_id],
                        total_steps=total_steps,
                    )

                async def _sem_execute(ps: _PipelineStep, step: Step) -> Step:
                    if sem:
                        # Emit step.queued only when this coroutine actually
                        # has to wait — keeps the event meaningful (signals
                        # capacity-bound delay, not just dispatch).
                        if sem.locked():
                            self._emit_step_queued(
                                run_id=run_id,
                                step=step,
                                ps=ps,
                                step_index=step_positions[step.step_id],
                                total_steps=total_steps,
                                reason="concurrency_limit",
                            )
                        async with sem:
                            return await self._execute_step_async(ps, step, config, _ctx_for(step))
                    return await self._execute_step_async(ps, step, config, _ctx_for(step))

                coro: Awaitable[list[Step]]
                if fail_fast:
                    coro = self._gather_fail_fast(
                        steps_and_models,
                        config,
                        _ctx_for,
                        semaphore=sem,
                        run_id=run_id,
                        total_steps=total_steps,
                    )
                else:
                    coro = asyncio.gather(
                        *(_sem_execute(ps, step) for ps, step in steps_and_models)
                    )

                if pipeline_timeout is not None:
                    remaining = pipeline_timeout - (time.monotonic() - started_at_mono)
                    if remaining <= 0:
                        msg = (
                            f"Pipeline timeout exceeded before concurrent launch"
                            f" (limit: {pipeline_timeout}s)"
                        )
                        raise PipelineTimeoutError(msg)
                    try:
                        concurrent_result = await asyncio.wait_for(coro, timeout=remaining)
                    except TimeoutError:
                        elapsed = time.monotonic() - started_at_mono
                        msg = (
                            f"Pipeline timeout exceeded after {elapsed:.1f}s"
                            f" (limit: {pipeline_timeout}s)"
                        )
                        raise PipelineTimeoutError(msg) from None
                else:
                    concurrent_result = await coro

                completed_steps = list(concurrent_result)

                # Concurrent mode: per-step callbacks fire after all steps complete.
                # Note: eager-transfer sinks won't see much speedup here since
                # all steps have already finished — chain/sequential is where
                # the hook pays off. We still fire it for API consistency.
                for idx, step_result in enumerate(completed_steps):
                    self._notify_sink_step_complete(sink, step_result, run_id, started_at_ts)
                    step_event = StepCompleteEvent(
                        step_index=idx,
                        total_steps=total_steps,
                        step=step_result,
                        elapsed_sec=time.monotonic() - started_at_mono,
                    )
                    self._call_user_callback(on_step_complete, step_event, "on_step_complete")
                    self._emit_step_complete_event(step_event, run_id)

            pipeline_result = self._finalize(completed_steps, sink, run_id, started_at_ts)
            logger.info(
                "Pipeline complete: status=%s, run_id=%s",
                pipeline_result.run.status,
                pipeline_result.run.run_id,
            )
            should_raise = _resolve_raise_on_failure(raise_on_failure)
            _maybe_raise_pipeline_error(pipeline_result, completed_steps, should_raise)
            return pipeline_result
        finally:
            # Guarantee on_run_end fires — covers timeouts, cancellation,
            # and bugs in _finalize. Synthesizes a result if we didn't reach it.
            self._emit_pipeline_end(
                pipeline_result or self._finalize(completed_steps, None, run_id, started_at_ts),
                run_id,
            )
            if spinner is not None:
                spinner.stop()

    def _make_failed_step(self, ps: _PipelineStep, exc: Exception) -> Step:
        """Create a FAILED step from an unhandled exception."""
        from genblaze_core.providers.base import _sanitize_error, classify_api_error

        step = self._build_step(ps)
        step.status = StepStatus.FAILED
        step.error = _sanitize_error(str(exc))
        step.error_code = classify_api_error(exc)
        return step

    async def _gather_fail_fast(
        self,
        steps_and_models: list[tuple[_PipelineStep, Step]],
        config: RunnableConfig | None,
        ctx_for: Any,
        *,
        semaphore: asyncio.Semaphore | None = None,
        run_id: str | None = None,
        total_steps: int = 0,
    ) -> list[Step]:
        """Run steps concurrently, cancelling remaining on first failure."""
        tasks: list[asyncio.Task] = []
        task_index: dict[asyncio.Task, int] = {}
        task_ps: dict[asyncio.Task, _PipelineStep] = {}

        async def _run(ps: _PipelineStep, step: Step) -> Step:
            if semaphore:
                if semaphore.locked() and run_id is not None:
                    # Mirror _sem_execute's queued emission so fail-fast and
                    # the gather path stay observably equivalent.
                    self._emit_step_queued(
                        run_id=run_id,
                        step=step,
                        ps=ps,
                        step_index=ctx_for(step).step_index,
                        total_steps=total_steps,
                        reason="concurrency_limit",
                    )
                async with semaphore:
                    return await self._execute_step_async(ps, step, config, ctx_for(step))
            return await self._execute_step_async(ps, step, config, ctx_for(step))

        for idx, (ps, step) in enumerate(steps_and_models):
            t = asyncio.create_task(_run(ps, step))
            tasks.append(t)
            task_index[t] = idx
            task_ps[t] = ps

        results: dict[int, Step] = {}
        pending: set[asyncio.Task] = set(tasks)
        should_cancel = False

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                idx = task_index[t]
                try:
                    result = t.result()
                except Exception as exc:
                    # Task raised — create a FAILED step instead of dropping it
                    logger.debug("Task %d raised an exception: %s", idx, exc)
                    result = self._make_failed_step(task_ps[t], exc)

                results[idx] = result

                if result.status == StepStatus.FAILED:
                    should_cancel = True

            if should_cancel:
                for p in pending:
                    p.cancel()
                # Await cancelled tasks so they can clean up resources
                await asyncio.gather(*pending, return_exceptions=True)
                pending.clear()
                break

        # Collect results from all tasks — cancelled tasks get FAILED placeholders
        for t in tasks:
            idx = task_index[t]
            if idx in results:
                continue
            if t.cancelled():
                step = self._build_step(task_ps[t])
                step.status = StepStatus.FAILED
                step.error = "Step cancelled due to fail-fast after prior step failure"
                results[idx] = step
            elif t.done():
                try:
                    results[idx] = t.result()
                except Exception as exc:
                    logger.debug("Task %d failed", idx)
                    results[idx] = self._make_failed_step(task_ps[t], exc)

        # Return in original order
        return [results[i] for i in sorted(results)]

    @staticmethod
    def _resolve_prompt(
        ps: _PipelineStep,
        prompt_or_vars: str | dict[str, str],
    ) -> str | None:
        """Resolve a step's prompt for a batch item.

        String items override all prompts. Dict items render PromptTemplate
        steps and leave plain string prompts unchanged.
        """
        if isinstance(prompt_or_vars, dict):
            if isinstance(ps.prompt, PromptTemplate):
                return ps.prompt.render(**prompt_or_vars)
            # Plain string or None — keep original
            return ps.prompt
        return prompt_or_vars

    @staticmethod
    def _apply_item_to_steps(
        steps: list[_PipelineStep], item: dict[str, Any]
    ) -> list[_PipelineStep]:
        """Build a per-item step list by merging ``item`` into step 0.

        Convention: ``item["prompt"]`` overrides step 0's prompt; every other
        key merges into step 0's ``params`` (per-item values win). Steps after
        index 0 are unchanged.

        Multi-step per-item params are intentionally out-of-scope here — the
        single-step batch case covers ~95% of asset-pack / aspect-ratio /
        seed-sweep workloads, and the position-keyed alternative
        (``item["steps"] = [{...}, {...}]``) is easy to add later without a
        breaking change.
        """
        if not steps:
            return steps
        head = steps[0]
        item_copy = dict(item)
        new_prompt = item_copy.pop("prompt", head.prompt)
        merged_params = {**head.params, **item_copy}
        new_head = replace(head, prompt=new_prompt, params=merged_params)
        return [new_head, *steps[1:]]

    @staticmethod
    def _validate_batch_args(
        prompts: list[str] | list[dict[str, str]] | None,
        items: list[dict[str, Any]] | None,
    ) -> None:
        if prompts is not None and items is not None:
            raise ValueError(
                "Pass either prompts= or items=, not both. items= is the "
                "richer per-iteration override; prompts= is shorthand when "
                "only the prompt varies."
            )
        if prompts is None and items is None:
            raise ValueError("batch_run requires either prompts= or items=.")

    def batch_run(
        self,
        prompts: list[str] | list[dict[str, str]] | None = None,
        *,
        items: list[dict[str, Any]] | None = None,
        max_concurrency: int = 5,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        raise_on_failure: bool | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        on_progress: Any = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
    ) -> list[PipelineResult]:
        """Execute the pipeline independently for each batch entry (sync).

        Each entry produces its own run with cloned steps. Results are
        returned in input order.

        Args:
            prompts: Per-item prompt overrides. Strings override step 0's
                prompt; dicts render ``PromptTemplate`` variables.
            items: Per-item params (incl. optional ``prompt``) merged into
                step 0. Use this for asset-pack / aspect-ratio / seed-sweep
                fan-outs where each iteration needs different ``seed``,
                ``aspect_ratio``, ``quality``, etc. Mutually exclusive with
                ``prompts=``.
            max_concurrency: Max concurrent pipeline executions (``abatch_run``).
            sink: Optional sink to write each run to.
            fail_fast: If True (default), stop each pipeline on first failure.
            raise_on_failure: See :meth:`run`. Applied per pipeline.
            timeout: Per-step timeout in seconds.
            max_retries: Per-step max retries.
            on_progress: Optional callback fired during provider poll loops.
            pipeline_timeout: End-to-end timeout in seconds for each pipeline.
            on_step_complete: Optional callback fired after each step completes.
        """
        import copy

        self._validate_batch_args(prompts, items)
        # Resolve the deprecation sentinel ONCE per batch — otherwise each
        # per-item ``pipe.run()`` would re-trigger the warning, swamping logs
        # for callers iterating over hundreds of items. The warning text
        # mentions ``BatchPipelineError`` so users see the right class name.
        resolved_raise = _resolve_raise_on_failure(
            raise_on_failure,
            exception_type="BatchPipelineError",
            surface="Pipeline.batch_run()",
        )

        def _build_pipe(rebuild_steps: list[_PipelineStep]) -> Pipeline:
            pipe = copy.copy(self)
            pipe._steps = rebuild_steps
            return pipe

        # Always pass raise_on_failure=False to per-item runs — collect every
        # result first, then synthesize ``BatchPipelineError`` after the loop
        # if requested. Aborting mid-batch on the first failed item would
        # silently lose the remaining results, which is rarely what callers
        # actually want for asset packs / aspect-ratio sweeps / A/B fan-outs.
        def _run_one(rebuild_steps: list[_PipelineStep]) -> PipelineResult:
            return _build_pipe(rebuild_steps).run(
                sink=sink,
                fail_fast=fail_fast,
                raise_on_failure=False,
                timeout=timeout,
                max_retries=max_retries,
                on_progress=on_progress,
                pipeline_timeout=pipeline_timeout,
                on_step_complete=on_step_complete,
            )

        results: list[PipelineResult] = []
        if items is not None:
            for item in items:
                results.append(_run_one(self._apply_item_to_steps(self._steps, item)))
        else:
            assert prompts is not None  # narrowed by _validate_batch_args
            for prompt_or_vars in prompts:
                results.append(
                    _run_one(
                        [
                            replace(ps, prompt=self._resolve_prompt(ps, prompt_or_vars))
                            for ps in self._steps
                        ]
                    )
                )
        _maybe_raise_batch_error(results, resolved_raise)
        return results

    async def abatch_run(
        self,
        prompts: list[str] | list[dict[str, str]] | None = None,
        *,
        items: list[dict[str, Any]] | None = None,
        max_concurrency: int = 5,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        raise_on_failure: bool | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        on_progress: Any = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
    ) -> list[PipelineResult]:
        """Execute the pipeline independently for each batch entry (async).

        Uses a semaphore to limit concurrency. Either ``prompts=`` or ``items=``
        must be supplied; see :meth:`batch_run` for the semantic difference.
        """
        import copy

        self._validate_batch_args(prompts, items)
        # Resolve the deprecation sentinel ONCE per batch — see batch_run.
        # Same collect-then-raise semantics: each per-item run is silenced,
        # ``BatchPipelineError`` is synthesized after gather completes.
        resolved_raise = _resolve_raise_on_failure(
            raise_on_failure,
            exception_type="BatchPipelineError",
            surface="Pipeline.abatch_run()",
        )
        sem = asyncio.Semaphore(max_concurrency)

        def _build_pipe(rebuild_steps: list[_PipelineStep]) -> Pipeline:
            pipe = copy.copy(self)
            pipe._steps = rebuild_steps
            return pipe

        async def _run(rebuild_steps: list[_PipelineStep]) -> PipelineResult:
            async with sem:
                return await _build_pipe(rebuild_steps).arun(
                    sink=sink,
                    fail_fast=fail_fast,
                    raise_on_failure=False,
                    timeout=timeout,
                    max_retries=max_retries,
                    on_progress=on_progress,
                    pipeline_timeout=pipeline_timeout,
                    on_step_complete=on_step_complete,
                )

        if items is not None:
            tasks = [_run(self._apply_item_to_steps(self._steps, item)) for item in items]
        else:
            assert prompts is not None
            tasks = [
                _run([replace(ps, prompt=self._resolve_prompt(ps, p)) for ps in self._steps])
                for p in prompts
            ]
        # ``return_exceptions=True`` is load-bearing — without it, a single
        # ``PipelineTimeoutError`` (or any other non-step exception) would
        # propagate immediately and cancel every other in-flight task,
        # silently breaking the "every batch item runs to completion"
        # promise. Per-item ``PipelineError``s are already suppressed via
        # ``raise_on_failure=False`` so they never reach this list; anything
        # we DO see here is a genuine pipeline-level error worth surfacing.
        results_or_excs = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[PipelineResult] = []
        for r in results_or_excs:
            if isinstance(r, BaseException):
                # Re-raise the first non-result item so callers see the
                # original error (timeout, validation, etc.) instead of a
                # generic BatchPipelineError. Every task has already finished
                # by the time we get here, so we're not aborting work in flight.
                raise r
            results.append(r)
        _maybe_raise_batch_error(results, resolved_raise)
        return results

    def resume_step(
        self,
        step: Step,
        prediction_id: Any,
        provider: BaseProvider,
        config: RunnableConfig | None = None,
    ) -> Step:
        """Resume a single in-flight step by prediction ID.

        Skips submit() and goes straight to poll→fetch_output. Use this
        to recover from a worker restart during long-running generations.
        """
        return provider.resume(prediction_id, step, config)

    async def aresume_step(
        self,
        step: Step,
        prediction_id: Any,
        provider: BaseProvider,
        config: RunnableConfig | None = None,
    ) -> Step:
        """Async version of resume_step()."""
        return await provider.aresume(prediction_id, step, config)

    def to_template(
        self,
        *,
        description: str | None = None,
        version: str | None = None,
        tags: list[str] | None = None,
    ) -> PipelineTemplate:
        """Export this pipeline's definition as a serializable template.

        Returns a PipelineTemplate that can be saved to JSON and
        instantiated later with different providers.
        """
        from genblaze_core.pipeline.template import PipelineTemplate, StepTemplate

        steps = [
            StepTemplate(
                provider_name=ps.provider.name,
                model=ps.model,
                prompt=ps.prompt
                if isinstance(ps.prompt, str)
                else (ps.prompt.template if isinstance(ps.prompt, PromptTemplate) else None),
                params=ps.params,
                modality=ps.modality,
                step_type=ps.step_type,
                fallback_models=ps.fallback_models,
                input_from=ps.input_from,
            )
            for ps in self._steps
        ]
        return PipelineTemplate(
            name=self._name,
            steps=steps,
            chain=self._chain,
            max_concurrency=self._max_concurrency,
            description=description,
            version=version,
            tags=tags or [],
        )
