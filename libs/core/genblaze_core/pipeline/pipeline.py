"""Pipeline — high-level fluent API for multi-step generation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from genblaze_core._utils import new_id, utc_now
from genblaze_core.builders.run_builder import RunBuilder
from genblaze_core.exceptions import GenblazeError, PipelineTimeoutError
from genblaze_core.models.enums import Modality, ProviderErrorCode, RunStatus, StepStatus, StepType
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.prompt_template import PromptTemplate
from genblaze_core.models.step import Step
from genblaze_core.observability.logger import StructuredLogger
from genblaze_core.pipeline.moderation import ModerationHook, ModerationResult
from genblaze_core.pipeline.result import PipelineResult, StepCompleteEvent
from genblaze_core.providers.base import BaseProvider
from genblaze_core.runnable.base import Runnable
from genblaze_core.runnable.config import RunnableConfig

logger = logging.getLogger("genblaze.pipeline")

if TYPE_CHECKING:
    from genblaze_core.models.asset import Asset
    from genblaze_core.pipeline.cache import StepCache
    from genblaze_core.sinks.base import BaseSink


class _PipelineStep:
    """A deferred step in a pipeline."""

    def __init__(
        self,
        provider: BaseProvider,
        model: str,
        prompt: str | PromptTemplate | None,
        params: dict,
        modality: Modality,
        step_type: StepType,
        fallback_models: list[str] | None = None,
        input_from: list[int] | None = None,
    ):
        self.provider = provider
        self.model = model
        self.prompt = prompt
        self.params = params
        self.modality = modality
        self.step_type = step_type
        self.fallback_models = fallback_models or []
        self.input_from = input_from


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
    ):
        self._name = name
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._parent_run_id: str | None = None
        self._steps: list[_PipelineStep] = []
        self._config: RunnableConfig | None = None
        self._cache: StepCache | None = None
        self._chain = chain
        self._max_concurrency = max_concurrency
        self._moderation = moderation
        self._slog = StructuredLogger("genblaze.pipeline") if structured_log else None

    def config(self, cfg: RunnableConfig) -> Pipeline:
        self._config = cfg
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
        **params,
    ) -> Pipeline:
        # Normalize scalar index to list for uniform handling
        normalized_from: list[int] | None = None
        if input_from is not None:
            normalized_from = [input_from] if isinstance(input_from, int) else list(input_from)
        self._steps.append(
            _PipelineStep(
                provider,
                model,
                prompt,
                params,
                modality,
                step_type,
                fallback_models,
                normalized_from,
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

    def _build_step(self, ps: _PipelineStep, inputs: list[Asset] | None = None) -> Step:
        """Create a Step model from a deferred pipeline step.

        Normalizes params via the provider's normalize_params() so cache keys
        and manifests use consistent parameter names. Extracts seed and
        negative_prompt from params into their top-level Step fields.
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
        # Extract Step-level fields that callers pass via **params
        seed = normalized.pop("seed", None)
        negative_prompt = normalized.pop("negative_prompt", None)

        # Persist pipeline graph info in metadata for faithful replay
        metadata: dict[str, Any] = {}
        if ps.fallback_models:
            metadata["_fallback_models"] = ps.fallback_models
        if ps.input_from is not None:
            metadata["_input_from"] = ps.input_from

        return Step(
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

        if self._slog:
            self._slog.info(
                "step.completed",
                step_id=step.step_id,
                provider=ps.provider.name,
                model=result.model,
                status=str(result.status),
                duration_ms=round(duration_ms, 1),
            )

        if self._cache is not None and result.status == StepStatus.SUCCEEDED:
            self._cache.put(cache_key_step, result)

        return result

    def _execute_step(self, ps: _PipelineStep, step: Step, config: RunnableConfig | None) -> Step:
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

        t0 = time.monotonic()
        result = ps.provider.invoke(step, config)

        result, cache_key_step = self._try_fallback_models(
            ps, step, result, config, ps.provider.invoke
        )
        duration_ms = (time.monotonic() - t0) * 1000

        return self._post_step(
            ps,
            step,
            result,
            cache_key_step,
            duration_ms,
            self._moderation.check_output if self._moderation else None,
        )

    async def _execute_step_async(
        self, ps: _PipelineStep, step: Step, config: RunnableConfig | None
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

        t0 = time.monotonic()
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
        duration_ms = (time.monotonic() - t0) * 1000

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
                return result
            if not mod_result.allowed:
                return self._apply_moderation_failure(result, mod_result, "post")

        return self._post_step(
            ps,
            step,
            result,
            cache_key_step,
            duration_ms,
            None,  # async moderation already handled above
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

        # Set run-level timestamps from pipeline execution
        if started_at_ts is not None:
            from datetime import UTC, datetime

            run_obj.started_at = datetime.fromtimestamp(started_at_ts, tz=UTC)
        run_obj.completed_at = utc_now()

        manifest = Manifest.from_run(run_obj)

        if sink is not None:
            # sink.write_run() recomputes the hash after asset transfer mutations
            sink.write_run(run_obj, manifest)

        return PipelineResult(run_obj, manifest)

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
        timeout: float | None = None,
        max_retries: int | None = None,
        on_progress: Any = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
        _config_override: RunnableConfig | None = None,
    ) -> PipelineResult:
        """Execute all steps synchronously and return a PipelineResult.

        Args:
            sink: Optional sink to write run data to.
            fail_fast: If True (default), stop on first failed step.
            timeout: Per-step timeout in seconds (builds RunnableConfig internally).
            max_retries: Per-step max retries (builds RunnableConfig internally).
            on_progress: Optional callback fired during provider poll loops.
            pipeline_timeout: End-to-end timeout in seconds for the entire pipeline.
            on_step_complete: Optional callback fired after each step completes.
                Receives a StepCompleteEvent.

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

        # Inject on_progress callback into config
        if on_progress is not None:
            config = RunnableConfig(**config) if config else RunnableConfig()
            config["on_progress"] = on_progress

        run_id = new_id()

        # Create scoped logger with run_id for structured log correlation
        slog = self._slog.with_context(run_id=run_id) if self._slog else None

        started_at_ts = time.time()
        started_at_mono = time.monotonic()
        logger.info("Starting pipeline %r with %d steps", self._name, len(self._steps))
        if slog:
            slog.info("pipeline.start", name=self._name, steps=len(self._steps))

        total_steps = len(self._steps)
        completed_steps: list[Step] = []
        prev_assets: list[Asset] = []
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
            step = self._build_step(ps, inputs)
            logger.debug(
                "Executing step %d/%d: %s/%s",
                i,
                len(self._steps),
                ps.provider.name,
                ps.model,
            )
            result = self._execute_step(ps, step, config)
            completed_steps.append(result)

            # Fire on_step_complete callback after each step
            if on_step_complete is not None:
                event = StepCompleteEvent(
                    step_index=i - 1,
                    total_steps=total_steps,
                    step=result,
                    elapsed_sec=time.monotonic() - started_at_mono,
                )
                on_step_complete(event)

            if result.status == StepStatus.SUCCEEDED:
                if self._chain:
                    prev_assets = list(result.assets)
            elif result.status == StepStatus.FAILED:
                # Clear chain inputs so the next step doesn't reuse stale outputs
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
        if slog:
            slog.info(
                "pipeline.complete",
                status=str(pipeline_result.run.status),
                run_id=pipeline_result.run.run_id,
            )
        return pipeline_result

    async def arun(
        self,
        *,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_concurrency: int | None = None,
        on_progress: Any = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
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
            pipeline_timeout: End-to-end timeout in seconds for the entire pipeline.
            on_step_complete: Optional callback fired after each step completes.
                Receives a StepCompleteEvent.

        Raises:
            GenblazeError: If no steps have been added to the pipeline.
            GenblazeError: If pipeline_timeout is exceeded.
        """
        if not self._steps:
            msg = "Pipeline has no steps. Add steps with .step() before calling .arun()."
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

        # Inject on_progress callback into config
        if on_progress is not None:
            config = RunnableConfig(**config) if config else RunnableConfig()
            config["on_progress"] = on_progress

        run_id = new_id()
        slog = self._slog.with_context(run_id=run_id) if self._slog else None

        started_at_ts = time.time()
        started_at_mono = time.monotonic()
        total_steps = len(self._steps)
        logger.info(
            "Starting async pipeline %r with %d steps",
            self._name,
            total_steps,
        )

        # input_from requires sequential execution (needs prior step results)
        has_input_from = any(ps.input_from is not None for ps in self._steps)

        if self._chain or has_input_from:
            # Sequential: each step's outputs feed the next step's inputs
            completed_steps: list[Step] = []
            prev_assets: list[Asset] = []
            for i, ps in enumerate(self._steps, 1):
                # Check pipeline-level timeout before each step
                if pipeline_timeout is not None:
                    elapsed = time.monotonic() - started_at_mono
                    if elapsed >= pipeline_timeout:
                        msg = (
                            f"Pipeline timeout exceeded after"
                            f" {elapsed:.1f}s"
                            f" (limit: {pipeline_timeout}s)"
                        )
                        raise PipelineTimeoutError(msg)

                inputs = self._resolve_inputs(ps, i - 1, completed_steps, prev_assets)
                step = self._build_step(ps, inputs)
                logger.debug(
                    "Executing step %d/%d: %s/%s",
                    i,
                    len(self._steps),
                    ps.provider.name,
                    ps.model,
                )
                result = await self._execute_step_async(ps, step, config)
                completed_steps.append(result)

                # Fire on_step_complete callback after each step
                if on_step_complete is not None:
                    event = StepCompleteEvent(
                        step_index=i - 1,
                        total_steps=total_steps,
                        step=result,
                        elapsed_sec=time.monotonic() - started_at_mono,
                    )
                    on_step_complete(event)

                if result.status == StepStatus.SUCCEEDED:
                    if self._chain:
                        prev_assets = list(result.assets)
                elif result.status == StepStatus.FAILED:
                    # Clear chain inputs so the next step doesn't reuse stale outputs
                    if self._chain:
                        prev_assets = []
                    logger.warning("Step %d failed: %s", i, result.error)
                    if fail_fast:
                        break
        else:
            # Concurrent: use as_completed + cancel on failure when fail_fast
            steps_and_models = [(ps, self._build_step(ps)) for ps in self._steps]

            # Check pipeline-level timeout before launching concurrent steps
            if pipeline_timeout is not None:
                elapsed = time.monotonic() - started_at_mono
                if elapsed >= pipeline_timeout:
                    msg = (
                        f"Pipeline timeout exceeded after {elapsed:.1f}s"
                        f" (limit: {pipeline_timeout}s)"
                    )
                    raise PipelineTimeoutError(msg)

            # Apply concurrency limit (param > constructor > unlimited)
            concurrency = max_concurrency or self._max_concurrency
            sem = asyncio.Semaphore(concurrency) if concurrency else None

            async def _sem_execute(ps: _PipelineStep, step: Step) -> Step:
                if sem:
                    async with sem:
                        return await self._execute_step_async(ps, step, config)
                return await self._execute_step_async(ps, step, config)

            # Build the concurrent coroutine
            if fail_fast:
                coro = self._gather_fail_fast(steps_and_models, config, semaphore=sem)
            else:
                coro = asyncio.gather(*(_sem_execute(ps, step) for ps, step in steps_and_models))

            # Enforce pipeline_timeout around the concurrent execution
            if pipeline_timeout is not None:
                remaining = pipeline_timeout - (time.monotonic() - started_at_mono)
                if remaining <= 0:
                    msg = (
                        f"Pipeline timeout exceeded before concurrent launch"
                        f" (limit: {pipeline_timeout}s)"
                    )
                    raise PipelineTimeoutError(msg)
                try:
                    result = await asyncio.wait_for(coro, timeout=remaining)
                except TimeoutError:
                    elapsed = time.monotonic() - started_at_mono
                    msg = (
                        f"Pipeline timeout exceeded after {elapsed:.1f}s"
                        f" (limit: {pipeline_timeout}s)"
                    )
                    raise PipelineTimeoutError(msg) from None
            else:
                result = await coro

            completed_steps = list(result) if not isinstance(result, list) else result

            # Note: in concurrent mode, callbacks fire after all steps complete
            # (not incrementally as each finishes). Use chain=True for per-step callbacks.
            if on_step_complete is not None:
                for idx, step_result in enumerate(completed_steps):
                    event = StepCompleteEvent(
                        step_index=idx,
                        total_steps=total_steps,
                        step=step_result,
                        elapsed_sec=time.monotonic() - started_at_mono,
                    )
                    on_step_complete(event)

        pipeline_result = self._finalize(completed_steps, sink, run_id, started_at_ts)
        logger.info(
            "Pipeline complete: status=%s, run_id=%s",
            pipeline_result.run.status,
            pipeline_result.run.run_id,
        )
        if slog:
            slog.info(
                "pipeline.complete",
                status=str(pipeline_result.run.status),
                run_id=pipeline_result.run.run_id,
            )
        return pipeline_result

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
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> list[Step]:
        """Run steps concurrently, cancelling remaining on first failure."""
        tasks: list[asyncio.Task] = []
        # Map task → original index + pipeline step to preserve ordering
        task_index: dict[asyncio.Task, int] = {}
        task_ps: dict[asyncio.Task, _PipelineStep] = {}

        async def _run(ps: _PipelineStep, step: Step) -> Step:
            if semaphore:
                async with semaphore:
                    return await self._execute_step_async(ps, step, config)
            return await self._execute_step_async(ps, step, config)

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

    def batch_run(
        self,
        prompts: list[str] | list[dict[str, str]],
        *,
        max_concurrency: int = 5,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        timeout: float | None = None,
        max_retries: int | None = None,
        on_progress: Any = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
    ) -> list[PipelineResult]:
        """Execute the pipeline independently for each prompt (sync).

        Each prompt gets its own run with cloned steps. Results are returned
        in the same order as the input prompts.

        Args:
            prompts: List of prompts (strings) or template variable dicts.
                String items override all step prompts. Dict items render
                PromptTemplate steps and leave plain string prompts unchanged.
            max_concurrency: Max concurrent pipeline executions (used in abatch_run).
            sink: Optional sink to write each run to.
            fail_fast: If True (default), stop each pipeline on first failed step.
            timeout: Per-step timeout in seconds.
            max_retries: Per-step max retries.
            on_progress: Optional callback fired during provider poll loops.
            pipeline_timeout: End-to-end timeout in seconds for each pipeline.
            on_step_complete: Optional callback fired after each step completes.
        """
        import copy

        results: list[PipelineResult] = []
        for prompt_or_vars in prompts:
            pipe = copy.copy(self)
            pipe._steps = [
                _PipelineStep(
                    ps.provider,
                    ps.model,
                    self._resolve_prompt(ps, prompt_or_vars),
                    ps.params,
                    ps.modality,
                    ps.step_type,
                    ps.fallback_models,
                    ps.input_from,
                )
                for ps in self._steps
            ]
            results.append(
                pipe.run(
                    sink=sink,
                    fail_fast=fail_fast,
                    timeout=timeout,
                    max_retries=max_retries,
                    on_progress=on_progress,
                    pipeline_timeout=pipeline_timeout,
                    on_step_complete=on_step_complete,
                )
            )
        return results

    async def abatch_run(
        self,
        prompts: list[str] | list[dict[str, str]],
        *,
        max_concurrency: int = 5,
        sink: BaseSink | None = None,
        fail_fast: bool = True,
        timeout: float | None = None,
        max_retries: int | None = None,
        on_progress: Any = None,
        pipeline_timeout: float | None = None,
        on_step_complete: Any = None,
    ) -> list[PipelineResult]:
        """Execute the pipeline independently for each prompt (async).

        Uses a semaphore to limit concurrency.

        Args:
            prompts: List of prompts (strings) or template variable dicts.
                String items override all step prompts. Dict items render
                PromptTemplate steps and leave plain string prompts unchanged.
            max_concurrency: Max concurrent pipeline executions.
            sink: Optional sink to write each run to.
            fail_fast: If True (default), stop each pipeline on first failed step.
            timeout: Per-step timeout in seconds.
            max_retries: Per-step max retries.
            on_progress: Optional callback fired during provider poll loops.
            pipeline_timeout: End-to-end timeout in seconds for each pipeline.
            on_step_complete: Optional callback fired after each step completes.
        """
        import copy

        sem = asyncio.Semaphore(max_concurrency)

        async def _run_one(prompt_or_vars: str | dict[str, str]) -> PipelineResult:
            async with sem:
                pipe = copy.copy(self)
                pipe._steps = [
                    _PipelineStep(
                        ps.provider,
                        ps.model,
                        self._resolve_prompt(ps, prompt_or_vars),
                        ps.params,
                        ps.modality,
                        ps.step_type,
                        ps.fallback_models,
                        ps.input_from,
                    )
                    for ps in self._steps
                ]
                return await pipe.arun(
                    sink=sink,
                    fail_fast=fail_fast,
                    timeout=timeout,
                    max_retries=max_retries,
                    on_progress=on_progress,
                    pipeline_timeout=pipeline_timeout,
                    on_step_complete=on_step_complete,
                )

        return list(await asyncio.gather(*(_run_one(p) for p in prompts)))

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
    ) -> PipelineTemplate:  # noqa: F821
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
