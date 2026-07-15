"""Pipeline — high-level fluent API for multi-step generation."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from genblaze_core._utils import (
    _SECRET_PATTERNS,
    new_id,
    normalize_tenant_id,
    sanitize_error,
    utc_now,
)
from genblaze_core.builders.run_builder import RunBuilder
from genblaze_core.exceptions import (
    BatchPipelineError,
    GenblazeError,
    PipelineError,
    PipelineTimeoutError,
    ProviderError,
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
from genblaze_core.pipeline.streaming import EmitterSlot, QueueEmitter, progress_to_stream_event
from genblaze_core.progress_display import Spinner, should_auto_enable
from genblaze_core.providers.base import BaseProvider
from genblaze_core.providers.validation import ValidationOutcome, ValidationResult
from genblaze_core.runnable.base import Runnable
from genblaze_core.runnable.config import RunnableConfig

logger = logging.getLogger("genblaze.pipeline")


class _InputResolutionError(GenblazeError):
    """Raised when declared upstream step outputs cannot be used as inputs."""


@dataclass(frozen=True)
class _PreparedStep:
    """A step plus the internal routing decision for runner dispatch."""

    step: Step
    prefailed: bool = False


_INPUT_RESOLUTION_FAILURE_REASON = "input_resolution"


# Sentinel for ``raise_on_failure``. ``None`` means "caller didn't pass it,
# warn about the upcoming default flip"; ``True`` / ``False`` are explicit.
# In genblaze-core 0.4.0 the default becomes ``True`` and the sentinel is
# removed.
_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION = "0.4.0"
_TEXT_METADATA_KEY = "text"
_MODERATION_SEGMENT_MAX_BYTES = 8 * 1024
_MODERATION_TOTAL_MAX_BYTES = 32 * 1024


def _coerce_str(value: str | bytes | bytearray) -> str:
    return value if isinstance(value, str) else value.decode("utf-8", errors="replace")


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
            text = _coerce_str(value)
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


def _bounded_moderation_segment(text: str, *, label: str) -> str | None:
    if not text:
        return None
    size = len(text.encode("utf-8"))
    if size > _MODERATION_SEGMENT_MAX_BYTES:
        raise ValueError(
            f"{label} exceeds moderation segment limit ({_MODERATION_SEGMENT_MAX_BYTES} bytes)"
        )
    return text


def _text_value(value: Any, *, label: str) -> str | None:
    """Convert a recognized text-bearing input field into moderation text."""
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        text = _coerce_str(value)
    else:
        raise ValueError(f"{label} must be a string or bytes, not structured metadata")
    return _bounded_moderation_segment(text, label=label)


def _input_text_payloads(inputs: Sequence[Asset]) -> list[str]:
    """Extract textual moderation payloads from input assets.

    The pipeline does not dereference input asset URLs here. It screens text
    carried in manifest-visible fields that providers commonly consume.
    """
    payloads: list[str] = []
    for asset in inputs:
        metadata_text = _text_value(
            asset.metadata.get(_TEXT_METADATA_KEY),
            label=f"input asset {asset.asset_id} metadata['text']",
        )
        if metadata_text is not None:
            payloads.append(metadata_text)
    return payloads


def _pre_moderation_payload(step: Step) -> str | None:
    """Build the pre-step moderation text from prompts plus textual inputs."""
    parts: list[str] = []
    if step.prompt is not None:
        prompt = _bounded_moderation_segment(step.prompt, label="prompt")
        if prompt is not None:
            parts.append(prompt)
    if step.negative_prompt is not None:
        negative_prompt = _bounded_moderation_segment(
            step.negative_prompt,
            label="negative_prompt",
        )
        if negative_prompt is not None:
            parts.append(negative_prompt)
    parts.extend(_input_text_payloads(step.inputs))
    if not parts:
        return None
    total_size = sum(len(part.encode("utf-8")) for part in parts) + max(0, len(parts) - 1) * 2
    if total_size > _MODERATION_TOTAL_MAX_BYTES:
        raise ValueError(
            f"moderation payload exceeds total limit ({_MODERATION_TOTAL_MAX_BYTES} bytes)"
        )
    return "\n\n".join(parts)


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
    # Caller-supplied Assets to seed step.inputs from outside the pipeline graph
    # (e.g., user-uploaded media for a multimodal chat step). Mutually exclusive
    # with input_from at construction. Defensive copy is taken in step().
    external_inputs: list[Asset] | None = None
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
        preflight: bool = True,
    ):
        self._name = name
        self._tenant_id = normalize_tenant_id(tenant_id)
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
        # Model preflight (default ON, soft-launch posture):
        # preflight (_check_step_capabilities + _validate_models) calls
        # validate_model() on each step's provider.
        # NOT_FOUND raises before any wire calls; OK_PROVISIONAL and
        # UNKNOWN_PERMISSIVE emit a single WARN per (provider, slug) per
        # Pipeline instance (the dedup set below resets on each Pipeline
        # construction). Hot paths can opt out via Pipeline(preflight=False)
        # or Pipeline.preflight(False).
        self._preflight: bool = preflight
        # Tracks (provider_name, slug) tuples already warned about so the
        # WARN log is one-per-pipeline-lifetime, mirroring the
        # _warned_deprecated dedup pattern in ModelRegistry.
        # The lock makes the check-then-add atomic across threads: arun()
        # offloads _validate_models via asyncio.to_thread, and abatch_run
        # clones share this set (shallow copy.copy), so several clones can
        # mutate it concurrently from different worker threads.
        self._warned_preflight: set[tuple[str, str]] = set()
        self._warned_preflight_lock = threading.Lock()
        # Arbitrary caller metadata merged into Run.metadata at _finalize()
        # time via Pipeline.metadata(**kwargs) (see #53). Additive across
        # calls, mirroring RunBuilder.meta()'s dict.update() semantics.
        self._run_metadata: dict[str, Any] = {}
        # Holds the "active" stream emitter for whichever thread/task is
        # currently inside stream()/astream()'s worker. ContextVar-backed
        # (not a plain mutable instance attribute) so concurrent
        # stream()/astream() calls on the SAME Pipeline instance don't
        # cross-deliver events (#79, #84). See EmitterSlot's docstring for
        # why this needs no additional locking.
        #
        # Built fresh per instance (NOT a class-level singleton) so a
        # DIFFERENT Pipeline instance run synchronously inside this one's
        # stream()/astream() worker (e.g. from a step provider, moderation
        # hook, or callback) never observes this instance's emitter — a
        # single shared ContextVar isolates concurrent calls on one instance
        # but does nothing to isolate distinct instances sharing the same
        # thread/task Context (#151).
        self._emitter_slot = EmitterSlot("genblaze_pipeline_emitter")
        # Tracer resolution: explicit arg wins; legacy structured_log=True maps
        # to LoggingTracer so existing callers keep their JSON event stream.
        if tracer is not None:
            self._tracer: Tracer = tracer
        elif structured_log:
            self._tracer = LoggingTracer()
        else:
            self._tracer = NoOpTracer()

    # --- Copy / serialization protocols ---------------------------------
    # The WARN-dedup ``threading.Lock`` added for #56 is neither copyable nor
    # picklable, so each protocol is handled explicitly:
    #   * copy.copy  -> __copy__: shallow, SHARES the lock + dedup set so a
    #     batch_run/abatch_run batch dedups each preflight WARN once as a unit.
    #   * copy.deepcopy / pickle -> __getstate__/__setstate__: a fully
    #     independent clone with a freshly built lock.
    # Defining __copy__ is required: without it, __getstate__/__setstate__
    # would also drive copy.copy and silently break the shared-lock contract.
    #
    # ``_emitter_slot`` is handled differently from the lock/dedup-set: EVERY
    # protocol gives the clone a brand-new ``EmitterSlot``, never a shared one
    # (also unpicklable, like the lock, but unlike the lock there's no known
    # use case for sharing it). A shallow ``copy.copy`` clone that shared the
    # slot would reintroduce #151's leak in a narrower shape — a
    # batch_run()/abatch_run() clone executing .run()/.arun() inside the same
    # thread/task Context as the pipeline that spawned it (e.g. batch_run()
    # invoked from a hook running inside this instance's own stream() worker)
    # would read this instance's emitter through the shared ContextVar and
    # leak its events into the same queue.

    def __copy__(self) -> Pipeline:
        """Shallow copy that shares the WARN-dedup lock and set across clones.

        The stream emitter slot is deliberately NOT shared — see the comment
        above the copy/serialization protocols block. ``_run_metadata`` also
        gets an independent dict (not the shared-by-reference default a plain
        ``__dict__.update()`` would give it) so a batch_run()/abatch_run()
        clone calling ``.metadata(...)`` on itself can never mutate the
        metadata the pipeline that spawned it will see.
        """
        clone = self.__class__.__new__(self.__class__)
        clone.__dict__.update(self.__dict__)
        clone._emitter_slot = EmitterSlot("genblaze_pipeline_emitter")
        clone._run_metadata = dict(self._run_metadata)
        return clone

    def __getstate__(self) -> dict:
        """Omit the unpicklable lock/emitter slot for pickle/deepcopy; rebuilt in setstate."""
        state = self.__dict__.copy()
        state.pop("_warned_preflight_lock", None)
        state.pop("_emitter_slot", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._warned_preflight_lock = threading.Lock()
        self._emitter_slot = EmitterSlot("genblaze_pipeline_emitter")

    @staticmethod
    def _reject_config_tenant(cfg: RunnableConfig | None) -> None:
        """Reject a per-call ``tenant_id`` in ``RunnableConfig``.

        ``RunnableConfig`` is a ``TypedDict`` and does not validate keys at
        runtime, so a config-level ``tenant_id`` would slip through as a plain
        dict key. It is never applied to the cache key or the Run, so accepting
        it silently would leak one tenant's cached output to another. Fail loudly
        and point at the supported path.
        """
        if cfg is not None and "tenant_id" in cfg:
            raise ValueError(
                "RunnableConfig does not support 'tenant_id'. Set the tenant on "
                "the pipeline instead: Pipeline(..., tenant_id=...) (see #68)."
            )

    def config(self, cfg: RunnableConfig) -> Pipeline:
        self._reject_config_tenant(cfg)
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

    def preflight(self, enabled: bool) -> Pipeline:
        """Toggle the model preflight validation phase.

        When enabled (the default), ``run()`` calls ``validate_model()`` on
        each step's provider before issuing any generation calls. ``NOT_FOUND``
        raises ``ProviderError(MODEL_ERROR)``; ``OK_PROVISIONAL`` and
        ``UNKNOWN_PERMISSIVE`` emit a WARN once per (provider, slug) per
        Pipeline instance; ``OK_AUTHORITATIVE`` is silent.

        Disable for hot paths where preflight overhead matters and the
        caller has already validated models out-of-band, or when running
        against fixtures that mock the upstream wire without backing
        ``discover_models()``.
        """
        self._preflight = enabled
        return self

    def from_result(self, result: PipelineResult) -> Pipeline:
        """Link this pipeline to a previous result for iteration lineage.

        Sets parent_run_id on the resulting run so manifests carry a pointer
        to the previous iteration. Does not affect the canonical hash.
        """
        self._parent_run_id = result.run.run_id
        return self

    @classmethod
    def ingest(
        cls,
        assets: Sequence[Asset],
        *,
        source: str,
        source_metadata: dict[str, Any] | None = None,
        sink: BaseSink | None = None,
        name: str | None = None,
        tenant_id: str | None = None,
        step_type: StepType = StepType.INGEST,
    ) -> PipelineResult:
        """Ingest a batch of assets and produce a provenance-complete manifest.

        Thin wrapper around :func:`genblaze_core.pipeline.ingest.ingest_assets`.
        Designed for non-generative workflows — DAM bulk import, RSS feed
        pull, UGC upload, podcast hosting, archival cross-tenancy moves —
        that need a manifest documenting the ingest event without going
        through the generation-shaped fluent ``.step(provider=…)`` API.

        Example::

            result = Pipeline.ingest(
                assets=[Asset(url="https://feed/ep1.mp3", media_type="audio/mp3")],
                source="rss",
                source_metadata={"feed_url": "https://example.com/feed.xml"},
                sink=storage_sink,
            )
            # result.manifest.canonical_hash documents the ingest event.

        Returns:
            :class:`PipelineResult` with one :class:`Step` per ingested
            asset (``step_type=INGEST``, ``provider=None``,
            ``model=source``) and a canonical-hashable manifest. The
            hash is stable across permuted input orders.
        """
        from genblaze_core.pipeline.ingest import ingest_assets

        return ingest_assets(
            assets,
            source=source,
            source_metadata=source_metadata,
            sink=sink,
            name=name,
            tenant_id=tenant_id,
            step_type=step_type,
        )

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
        external_inputs: list[Asset] | None = None,
        expected_duration_sec: float | None = None,
        params: dict[str, Any] | None = None,
        **extra_params: Any,
    ) -> Pipeline:
        """Add a step to the pipeline.

        Args:
            external_inputs: Caller-held Assets to seed ``Step.inputs`` from
                outside the pipeline graph (e.g., user-uploaded media for a
                multimodal chat step on position 0). Mutually exclusive with
                ``input_from``. Pass an Asset with ``sha256`` populated for
                stable cache keys and manifest canonical hashes; otherwise
                rotating URLs (e.g., presigned) will cause both to drift.
                Provider must declare ``accepts_chain_input=True`` in its
                ``ProviderCapabilities``.
            expected_duration_sec: Caller-supplied ETA hint (seconds), echoed
                on the ``step.started`` event so consumers can render progress
                UIs. The SDK does not synthesize this — supply your own median
                from observed runs. Stale values produce worse UX than
                omitting the field; treat as informational.
            params: Provider-specific parameters as a dict. Equivalent to
                passing the same keys as top-level kwargs — use whichever
                reads better at the call site. If a key appears in both,
                the top-level kwarg wins (issue #133).
            **extra_params: Provider-specific parameters as top-level kwargs
                (e.g. ``duration=10``). Merged with ``params=`` above.
        """
        # Explicit params= dict and **extra_params kwargs both feed
        # Step.params; kwargs win on key collision since they're the more
        # specific, call-site-local override.
        merged_params: dict[str, Any] = dict(params) if params else {}
        merged_params.update(extra_params)

        # Reject reserved param names that would silently land in Step.params.
        # `inputs=` / `input=` is the natural-but-wrong name a user trying to
        # seed Step.inputs would reach for; without this guard they get
        # swallowed into params, normalized as a model param, and either
        # rejected by the upstream provider or — worse — embedded in the
        # manifest as part of Step.params, drifting the canonical hash.
        for reserved in ("inputs", "input"):
            if reserved in merged_params:
                raise GenblazeError(
                    f"'{reserved}=' is not a valid step() kwarg — did you mean "
                    f"'external_inputs=' (a list of caller-held Assets)? See "
                    f"docs/features/pipelines.md for the three input mechanisms."
                )
        # Normalize scalar index to list for uniform handling
        normalized_from: list[int] | None = None
        if input_from is not None:
            normalized_from = [input_from] if isinstance(input_from, int) else list(input_from)
        # external_inputs / input_from are mutually exclusive at construction.
        # Single use case is "step 0 has no prior step to reference," which
        # doesn't compose with input_from. Easy to relax later (append
        # semantics) if a real composition use case shows up.
        if external_inputs and normalized_from is not None:
            raise GenblazeError(
                "external_inputs= and input_from= are mutually exclusive. "
                "Use external_inputs= to inject caller-held Assets; use "
                "input_from= to reference assets from a prior step."
            )
        # Defensive copy so post-construction mutation of the caller's list
        # (e.g. assets.append(...)) doesn't bleed into the deferred step.
        normalized_external: list[Asset] | None = None
        if external_inputs:
            normalized_external = list(external_inputs)
            # Cache stability + manifest canonical-hash both depend on
            # Asset.sha256. Without it, cache.py falls back to the asset URL,
            # which rotates for presigned URLs — silently degrading dedup AND
            # drifting the manifest canonical hash across reruns. Warn loud.
            for a in normalized_external:
                if not getattr(a, "sha256", None):
                    logger.warning(
                        "external_inputs Asset has no sha256 (url=%s); "
                        "step cache key and manifest canonical hash will "
                        "be unstable across reruns if the URL rotates "
                        "(e.g., presigned). Compute sha256 before passing.",
                        getattr(a, "url", "<unknown>"),
                    )
        self._steps.append(
            _PipelineStep(
                provider=provider,
                model=model,
                prompt=prompt,
                params=merged_params,
                modality=modality,
                step_type=step_type,
                fallback_models=fallback_models or [],
                input_from=normalized_from,
                external_inputs=normalized_external,
                expected_duration_sec=expected_duration_sec,
            )
        )
        return self

    def _check_step_capabilities(self) -> None:
        """Validate modality support and chain-input compatibility for every step.

        CPU-only checks (no network I/O). Separated from ``_validate_models`` so
        the fast, cheap assertions can run inline on the event loop while the
        slow model-preflight can be offloaded to a thread in async contexts.
        """
        for i, ps in enumerate(self._steps):
            caps = ps.provider.get_capabilities()
            if caps is None:
                continue

            # Modality support
            if caps.supported_modalities and ps.modality not in caps.supported_modalities:
                supported = ", ".join(str(m) for m in caps.supported_modalities)
                msg = (
                    f"Step {i} ({ps.provider.name}): modality '{ps.modality}' not supported."
                    f" Supported: [{supported}]"
                )
                raise GenblazeError(msg)

            # In chain mode, downstream steps must accept chain inputs.
            # external_inputs= bypasses chain mode (the caller is supplying
            # Assets directly) but still reads from step.inputs at runtime,
            # so the provider must declare it accepts that surface.
            has_external = bool(ps.external_inputs)
            receives_chain = has_external or (self._chain and i > 0) or ps.input_from is not None
            if receives_chain and not caps.accepts_chain_input:
                msg = (
                    f"Step {i} ({ps.provider.name}): receives inputs (external_inputs,"
                    f" input_from, or chain mode) but provider does not accept input"
                    f" assets. Set accepts_chain_input=True in ProviderCapabilities or"
                    f" remove the input source."
                )
                raise GenblazeError(msg)

    def _validate_steps(self) -> None:
        """Validate step capabilities and run model preflight. Fails loud on mismatches."""
        self._check_step_capabilities()

        # Model preflight runs after capability validation so a misconfigured
        # step (wrong modality, missing chain support) surfaces a clear error
        # before the slug-validity question even comes up.
        if self._preflight:
            self._validate_models()

    async def _validate_steps_async(self) -> None:
        """Async variant of ``_validate_steps`` that keeps the event loop free.

        Cheap capability checks run synchronously (CPU-only, negligible). The
        network-bound ``_validate_models`` phase (ThreadPoolExecutor + provider
        discovery fetches) is offloaded via ``asyncio.to_thread`` so concurrent
        coroutines keep running during the preflight window.

        The single-flight discovery/probe cache inside each provider makes
        off-thread dispatch safe -- concurrent fetches deduplicate at the
        provider level.

        Cancellation note: cancelling the awaiting task raises CancelledError
        here, but the offloaded worker runs to completion (Python can't
        interrupt the blocking fetch). The only residue is benign -- cache
        fill and a possible WARN may land after the caller has gone.
        """
        # Capabilities before model preflight (see _validate_steps): a
        # misconfigured step must surface before the slug-validity question.
        # Cheap modality/chain-input checks run inline, no I/O.
        self._check_step_capabilities()

        # Network-bound model preflight: offload so the event loop stays free.
        if self._preflight:
            await asyncio.to_thread(self._validate_models)

    def _validate_models(self) -> None:
        """Preflight: validate every step's model in parallel.

        ``BaseProvider.validate_model()`` may issue a discovery fetch or a
        family probe — both are network operations. We dispatch them via
        ``concurrent.futures.ThreadPoolExecutor`` so the sync ``Pipeline.run()``
        stays compatible with FastAPI / Jupyter / threaded daemons. The
        single-flight discovery cache deduplicates concurrent fetches at the
        provider level, so multiple steps targeting the same provider share
        one round-trip.

        Behavior on outcome:
        - ``NOT_FOUND``: raise ``ProviderError(MODEL_ERROR)`` immediately.
        - ``OK_PROVISIONAL``: WARN once per (provider, slug) per Pipeline instance.
        - ``UNKNOWN_PERMISSIVE``: WARN once per (provider, slug) per Pipeline instance.
        - ``OK_AUTHORITATIVE``: silent.
        """
        if not self._steps:
            return

        # Bound concurrency at 8 — empirically enough to overlap 8 different
        # providers' discovery fetches; wider would just contend on CPython
        # GIL during regex matching for the family-only path.
        max_workers = min(8, len(self._steps))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(self._safe_validate_model, ps): (i, ps)
                for i, ps in enumerate(self._steps)
            }
            for fut in as_completed(futures):
                i, ps = futures[fut]
                # _safe_validate_model never raises — it returns a synthetic
                # ValidationResult on internal exceptions so a flaky validator
                # cannot block the entire pipeline.
                result = fut.result()
                self._handle_validation(i, ps, result)

    @staticmethod
    def _safe_validate_model(ps: _PipelineStep) -> ValidationResult:
        try:
            return ps.provider.validate_model(ps.model)
        except Exception as exc:
            # Validator threw — degrade to permissive rather than fail
            # closed. The actual upstream call will surface the real error
            # if there is one. Logged at DEBUG: this is expected for
            # offline tests that mock the wire without backing
            # discover_models().
            logger.debug(
                "validate_model(%s/%s) raised: %s — falling through",
                ps.provider.name,
                ps.model,
                exc,
            )
            return ValidationResult.unknown_permissive(detail=f"validator raised: {exc}")

    def _handle_validation(self, i: int, ps: _PipelineStep, result: ValidationResult) -> None:
        if result.outcome is ValidationOutcome.NOT_FOUND:
            suggestions = (
                f" Did you mean: {', '.join(result.suggested_slugs[:3])}?"
                if result.suggested_slugs
                else ""
            )
            # Surface ``result.detail`` in the error so cached-DEAD
            # verdicts and stale-cache hints reach the user's traceback.
            # Operators investigating "why is preflight failing for a
            # slug I just confirmed live?" need to see the cache-staleness
            # signal here rather than tail the WARN logs separately.
            detail = f" ({result.detail})" if result.detail else ""
            recovery = (
                " Try `provider.validate_model(slug, refresh=True)` "
                "to re-probe if the cached verdict may be stale."
            )
            raise ProviderError(
                f"Step {i} ({ps.provider.name}): model {ps.model!r} not found "
                f"in upstream catalog.{suggestions}{detail}{recovery} "
                f"See docs/migration/registry-decoupling.md.",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )

        key = (ps.provider.name, ps.model)
        if result.outcome is ValidationOutcome.OK_PROVISIONAL:
            if self._warn_once(key):
                logger.warning(
                    "preflight.provisional step=%d provider=%s model=%s "
                    "family=%s detail=%s — liveness unverifiable; "
                    "failures will surface mid-pipeline",
                    i,
                    ps.provider.name,
                    ps.model,
                    result.family_name,
                    result.detail,
                )
        elif result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE:
            if self._warn_once(key):
                logger.warning(
                    "preflight.unknown step=%d provider=%s model=%s — "
                    "no family matched; permissive fallback applies",
                    i,
                    ps.provider.name,
                    ps.model,
                )
        # OK_AUTHORITATIVE: silent.

    def _warn_once(self, key: tuple[str, str]) -> bool:
        """Atomically claim ``key`` for a one-time WARN; return True the first
        time it is seen, False thereafter.

        The check-then-add runs under a lock so concurrent _validate_models
        runs (abatch_run clones share this set via shallow copy.copy, each
        dispatched to its own asyncio.to_thread worker) can't both emit the
        same WARN. Logging is left to the caller so the loop's I/O happens
        outside the lock.
        """
        with self._warned_preflight_lock:
            if key in self._warned_preflight:
                return False
            self._warned_preflight.add(key)
            return True

    def _resolve_inputs(
        self,
        ps: _PipelineStep,
        step_index: int,
        completed_steps: list[Step],
        prev_assets: list[Asset],
    ) -> list[Asset] | None:
        """Determine inputs for a step.

        Precedence: external_inputs > input_from > chain mode > none.
        external_inputs short-circuits both input_from (rejected at construction)
        and chain mode (the caller is supplying Assets directly, overriding the
        implicit chain).
        """
        if ps.external_inputs:
            # Defensive copy was already taken at step() time; return as-is.
            return ps.external_inputs
        if ps.input_from is not None:
            for idx in ps.input_from:
                if idx < 0 or idx >= step_index:
                    msg = (
                        f"input_from index {idx} is out of range for step {step_index}"
                        f" (only {step_index} prior steps completed)"
                    )
                    raise _InputResolutionError(msg)
            assets: list[Asset] = []
            for idx in ps.input_from:
                upstream = completed_steps[idx]
                if upstream.status != StepStatus.SUCCEEDED:
                    upstream_label = (
                        "failed upstream step"
                        if upstream.status == StepStatus.FAILED
                        else "upstream step"
                    )
                    msg = (
                        f"input_from index {idx} for step {step_index} points to "
                        f"{upstream_label} {idx} ({upstream.step_id}) with status "
                        f"{upstream.status.value}"
                    )
                    raise _InputResolutionError(msg)
                if not upstream.assets:
                    msg = (
                        f"input_from index {idx} for step {step_index} resolved no assets "
                        f"from upstream step {idx}"
                    )
                    raise _InputResolutionError(msg)
                assets.extend(upstream.assets)
            return assets
        if self._chain:
            return prev_assets
        return None

    def _build_or_prefail_step(
        self,
        ps: _PipelineStep,
        step_index: int,
        completed_steps: list[Step],
        prev_assets: list[Asset],
        *,
        step_id: str | None = None,
    ) -> _PreparedStep:
        """Build a runnable step or a FAILED step for unavailable input_from assets."""
        try:
            inputs = self._resolve_inputs(ps, step_index, completed_steps, prev_assets)
        except _InputResolutionError as exc:
            step = self._build_input_resolution_failure_step(ps, exc, step_id=step_id)
            return _PreparedStep(step=step, prefailed=True)
        return _PreparedStep(step=self._build_step(ps, inputs, step_id=step_id))

    def _build_input_resolution_failure_step(
        self,
        ps: _PipelineStep,
        error: _InputResolutionError,
        *,
        step_id: str | None = None,
    ) -> Step:
        """Build a failed Step when declared input dependencies are unavailable."""
        if isinstance(ps.prompt, PromptTemplate):
            msg = (
                "Step prompt is a PromptTemplate but was not rendered. "
                "Use batch_run() with dicts or call template.render() "
                "before passing to step()."
            )
            raise GenblazeError(msg)

        # Input resolution failed before a provider input list exists, so this
        # intentionally records no Step.inputs and never calls provider hooks
        # such as normalize_params(). Exception failures use _make_failed_step(),
        # which preserves caller-supplied external_inputs.
        params = dict(ps.params)
        _reject_credentials_in_params(params, ps.provider.name, ps.model)
        seed = params.pop("seed", None)
        negative_prompt = params.pop("negative_prompt", None)
        metadata = self._build_step_metadata(ps)
        metadata["failure_reason"] = _INPUT_RESOLUTION_FAILURE_REASON
        metadata["provider_invoked"] = False
        now = utc_now()
        step_kwargs: dict[str, Any] = dict(
            provider=ps.provider.name,
            model=ps.model,
            prompt=ps.prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            params=params,
            modality=ps.modality,
            step_type=ps.step_type,
            status=StepStatus.FAILED,
            inputs=[],
            assets=[],
            error=sanitize_error(str(error)),
            error_code=ProviderErrorCode.INVALID_INPUT,
            started_at=now,
            completed_at=now,
            metadata=metadata,
        )
        if step_id is not None:
            step_kwargs["step_id"] = step_id
        return Step(**step_kwargs)

    def _emit_tracer_step_start(self, step: Step, ctx: _StepContext) -> None:
        """Emit the tracer start hook with the shared step lifecycle shape."""
        safe_call(
            self._tracer,
            "on_step_start",
            ctx.run_id,
            step,
            step_index=ctx.step_index,
            total_steps=ctx.total_steps,
        )

    def _emit_tracer_step_end(
        self,
        step: Step,
        ctx: _StepContext,
        *,
        duration_ms: float,
    ) -> None:
        """Emit the tracer end hook with the shared step lifecycle shape."""
        safe_call(
            self._tracer,
            "on_step_end",
            ctx.run_id,
            step,
            duration_ms=duration_ms,
            step_index=ctx.step_index,
        )

    def _record_prefailed_step(self, step: Step, ctx: _StepContext) -> Step:
        """Emit tracer lifecycle hooks for a step that failed before provider invoke."""
        logger.warning(
            "step.prefailed run_id=%s step_index=%d reason=%s provider=%s model=%s "
            "error_code=%s provider_invoked=false",
            ctx.run_id,
            ctx.step_index,
            step.metadata.get("failure_reason"),
            step.provider,
            step.model,
            step.error_code.value if step.error_code else None,
        )
        self._emit_tracer_step_start(step, ctx)
        self._emit_tracer_step_end(step, ctx, duration_ms=0.0)
        return step

    def _build_step_metadata(self, ps: _PipelineStep) -> dict[str, Any]:
        """Build persisted pipeline graph metadata for a deferred step."""
        metadata: dict[str, Any] = {}
        if ps.fallback_models:
            metadata["_fallback_models"] = ps.fallback_models
        if ps.input_from is not None:
            metadata["_input_from"] = ps.input_from
        return metadata

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
        metadata = self._build_step_metadata(ps)

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
        label = "prompt/input" if stage == "pre" else "output"
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
            self._cache.put(cache_key_step, result, tenant_id=self._tenant_id)

        if result.status == StepStatus.FAILED and result.error:
            # Providers usually sanitize their own failures. This pipeline
            # boundary backstops adapters that return failed Steps directly so
            # manifests, logs, and stream events share the same redaction cap.
            result.error = sanitize_error(result.error)

        return result

    def _execute_step(
        self,
        ps: _PipelineStep,
        step: Step,
        config: RunnableConfig | None,
        ctx: _StepContext,
    ) -> Step:
        """Execute a single step with moderation, caching, and fallback models."""
        if self._moderation is not None:
            try:
                moderation_payload = _pre_moderation_payload(step)
            except ValueError as exc:
                step.status = StepStatus.FAILED
                step.error = f"Moderation input error: {exc}"
                step.error_code = ProviderErrorCode.INVALID_INPUT
                return step
            if moderation_payload is not None:
                try:
                    mod_result = self._moderation.check_prompt(moderation_payload, step.params)
                except Exception as exc:
                    step.status = StepStatus.FAILED
                    step.error = f"Moderation hook error: {exc}"
                    step.error_code = ProviderErrorCode.UNKNOWN
                    return step
                if not mod_result.allowed:
                    return self._apply_moderation_failure(step, mod_result, "pre")

        if self._cache is not None:
            cached = self._cache.get(step, tenant_id=self._tenant_id)
            if cached is not None:
                return cached

        self._emit_tracer_step_start(step, ctx)
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
            self._emit_tracer_step_end(
                final,
                ctx,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def _execute_step_async(
        self,
        ps: _PipelineStep,
        step: Step,
        config: RunnableConfig | None,
        ctx: _StepContext,
    ) -> Step:
        """Execute a single step asynchronously with moderation, caching, and fallback."""
        if self._moderation is not None:
            try:
                moderation_payload = _pre_moderation_payload(step)
            except ValueError as exc:
                step.status = StepStatus.FAILED
                step.error = f"Moderation input error: {exc}"
                step.error_code = ProviderErrorCode.INVALID_INPUT
                return step
            if moderation_payload is not None:
                try:
                    mod_result = await self._moderation.acheck_prompt(
                        moderation_payload, step.params
                    )
                except Exception as exc:
                    step.status = StepStatus.FAILED
                    step.error = f"Moderation hook error: {exc}"
                    step.error_code = ProviderErrorCode.UNKNOWN
                    return step
                if not mod_result.allowed:
                    return self._apply_moderation_failure(step, mod_result, "pre")

        if self._cache is not None:
            cached = self._cache.get(step, tenant_id=self._tenant_id)
            if cached is not None:
                return cached

        self._emit_tracer_step_start(step, ctx)
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
            self._emit_tracer_step_end(
                final,
                ctx,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    def _finalize(
        self,
        completed_steps: list[Step],
        sink: BaseSink | None,
        run_id: str,
        started_at_ts: float | None = None,
        *,
        force_status: RunStatus | None = None,
    ) -> PipelineResult:
        """Build run, manifest, and write to sink.

        ``force_status`` overrides the inferred COMPLETED/FAILED status.
        Used by run()/arun()'s exception fallback so a run that aborted
        before reaching normal completion is never reported as COMPLETED,
        regardless of how many of its steps happened to succeed before the
        abort (#85).
        """
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

        if force_status is not None:
            builder.status(force_status)
        else:
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
        """Install (or clear) the stream event emitter for the calling
        thread/task, returning the prior one.

        Storage is ``_emitter_slot`` (contextvars-backed), not an instance
        attribute — isolating concurrent stream()/astream() calls on the
        same Pipeline instance from each other (#79, #84). Public so
        composable runners (e.g. AgentLoop) can pipe pipeline events into
        their own event stream without poking private state.
        """
        return self._emitter_slot.set(emitter)

    @property
    def _event_emitter(self) -> QueueEmitter | None:
        return self._emitter_slot.get()

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
        merged: RunnableConfig = RunnableConfig(**config) if config else RunnableConfig()
        merged["run_id"] = run_id
        if not needs_install:
            return merged

        def _composite_progress(ev: Any) -> None:
            # User callback first so their side effects see the raw ProgressEvent
            # ordering; emit event to tracer/stream regardless of callback outcome.
            self._call_user_callback(user_on_progress, ev, "on_progress")
            self._emit_event(progress_to_stream_event(ev, run_id))

        def _composite_retry(ev: Any) -> None:
            # ``ev`` is a fully-formed StepRetriedEvent — emit verbatim.
            self._call_user_callback(user_on_retry, ev, "on_retry")
            self._emit_event(ev)

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
        # Tracer on_step_end is paired in _execute_step, _execute_step_async,
        # and _record_prefailed_step via _emit_tracer_step_end; emitter-only here.
        # run_id is passed explicitly rather than relying on the emitter's own
        # (never-set) run_id — stream()/astream() build the emitter before
        # the run id exists (#87).
        if self._event_emitter is not None:
            self._event_emitter.on_step_complete(step_event, run_id)

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

    def _emit_pipeline_end(
        self, result: PipelineResult, run_id: str, *, message: str | None = None
    ) -> None:
        """Emit the terminal pipeline event.

        ``message`` overrides the step-derived ``error_summary()`` — used by
        run()/arun()'s exception fallback to surface the abort reason (e.g.
        a timeout) even when no step recorded an error (#85).
        """
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
                message=message if message is not None else result.error_summary(),
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
        and any post-break exception in the pipeline is suppressed. The
        emitter is closed as soon as we detect the early break, so the
        abandoned worker's remaining ``put()`` calls become no-ops instead
        of piling onto a queue nobody will ever drain (#74).
        """
        import contextvars
        import queue as _queue
        import threading

        from genblaze_core.pipeline.streaming import drain_queue_sync

        q: _queue.Queue = _queue.Queue()
        emitter = QueueEmitter(q, include_heartbeats=heartbeats)

        exc_box: list[BaseException] = []
        done = threading.Event()

        def _worker() -> None:
            # Installed inside the worker thread so it lands in this
            # thread's own Context — isolated from a concurrent stream()
            # call's worker thread on the same Pipeline instance (#84).
            self.attach_emitter(emitter)
            try:
                self.run(**run_kwargs)
            except BaseException as exc:  # noqa: BLE001 — propagate via queue
                exc_box.append(exc)
            finally:
                emitter.close()
                done.set()

        # Run the worker inside its own throwaway Context — the same trick
        # asyncio.create_task() already gets for free — so the attach_emitter
        # install above can never leak onto the underlying OS thread itself.
        # Safe today regardless (a fresh Thread's default context is already
        # empty), but this keeps correctness structural rather than
        # incidental if a future caller ever reuses worker threads via a
        # pooled executor instead of spawning one per stream() call.
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(_worker,), daemon=True, name="genblaze-stream")
        t.start()
        try:
            yield from drain_queue_sync(q)
        finally:
            if done.is_set():
                t.join()  # fast — worker already returned
                if exc_box:
                    raise exc_box[0]
            else:
                # Consumer broke early; the worker keeps running as a
                # daemon thread until the pipeline naturally completes, but
                # close the emitter now so it stops enqueuing further
                # events (#74).
                emitter.close()

    async def astream(self, *, heartbeats: bool = True, **run_kwargs: Any):
        """Async version of :meth:`stream`.

        Args:
            heartbeats: See :meth:`stream`. Default ``True``.

        Early break: cancels the worker task so in-flight provider calls
        unwind at their next await point. Any post-break exception from
        the pipeline is suppressed. The emitter is closed before
        cancellation so any event racing the cancel becomes a no-op instead
        of reaching an abandoned queue (#74).
        """
        from genblaze_core.pipeline.streaming import drain_queue_async

        q: asyncio.Queue = asyncio.Queue()
        emitter = QueueEmitter(q, include_heartbeats=heartbeats)

        async def _worker() -> None:
            # Installed inside the task's own (copied) context — isolated
            # from a concurrent astream() call's task on the same Pipeline
            # instance (#84).
            self.attach_emitter(emitter)
            try:
                await self.arun(**run_kwargs)
            finally:
                emitter.close()

        task = asyncio.create_task(_worker())
        try:
            async for ev in drain_queue_async(q):
                yield ev
        finally:
            if task.done():
                await task  # re-raises if worker failed
            else:
                # Consumer broke early — close first so anything racing the
                # cancellation becomes a no-op (#74), then cancel the worker.
                emitter.close()
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

    @staticmethod
    def _close_sink_quietly(sink: BaseSink | None) -> None:
        """Release a sink's run-scoped resources, swallowing teardown errors.

        No-op when ``sink`` is None or the sink opts out via
        ``_close_with_run = False`` (process-scoped/fire-and-forget sinks such
        as WebhookSink manage their own lifecycle and must not be closed — or
        joined — by the pipeline). A failed close is logged, never raised, so it
        cannot mask the run's real result. Shared by run()/arun()/batch teardown.
        """
        if sink is None or not getattr(sink, "_close_with_run", True):
            return
        try:
            sink.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup; log and continue
            logger.warning(
                "%s.close() raised during pipeline teardown; resources may not have been released",
                type(sink).__name__,
                exc_info=True,
            )

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
        _owns_sink: bool = True,
    ) -> PipelineResult:
        """Execute all steps synchronously and return a PipelineResult.

        Args:
            sink: Optional sink to write run data to. Sinks with run-scoped
                resources (e.g. ObjectStorageSink) are closed automatically when
                the run finishes (their ``close()`` fires in a ``finally`` block),
                so such a sink is spent afterward — construct a fresh one per run.
                Fire-and-forget sinks like WebhookSink opt out
                (``_close_with_run = False``) and stay open/reusable.
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
        # Preflight runs before the main try/finally. When run() owns the sink
        # it must still close it on an early preflight/validation failure
        # (issue #57) — share the same teardown helper, no duplicated close.
        try:
            # Reject an invalid config-level tenant before model preflight (which
            # may do network work), so a bad invoke(config={"tenant_id": ...})
            # fails fast.
            self._reject_config_tenant(config)
            self._validate_steps()
        except BaseException:
            if _owns_sink:
                self._close_sink_quietly(sink)
            raise

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
        abort_message: str | None = None
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

                ctx = _StepContext(run_id=run_id, step_index=i - 1, total_steps=total_steps)
                prepared = self._build_or_prefail_step(
                    ps,
                    i - 1,
                    completed_steps,
                    prev_assets,
                    step_id=step_ids[i - 1],
                )
                step = prepared.step
                logger.debug(
                    "Executing step %d/%d: %s/%s",
                    i,
                    len(self._steps),
                    ps.provider.name,
                    ps.model,
                )
                self._emit_step_start(ctx, step, ps)
                if spinner is not None:
                    spinner.step_starting(
                        ps.provider.name,
                        ps.model,
                        prompt=step.prompt,
                        step_index=i - 1,
                        total=total_steps,
                    )
                # Input-resolution prefail handling is shared; the runner call
                # site only differs in sync vs async provider invocation.
                if prepared.prefailed:
                    result = self._record_prefailed_step(step, ctx)
                else:
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
        except BaseException as exc:
            # Only stash a synthetic abort message when _finalize never ran
            # for this run (pipeline_result still None) — e.g. a mid-loop
            # PipelineTimeoutError. A PipelineError raised by
            # _maybe_raise_pipeline_error AFTER a normal _finalize already
            # carries accurate step-level errors via error_summary().
            if pipeline_result is None:
                abort_message = sanitize_error(str(exc))
            raise
        finally:
            # Guarantee on_run_end fires — covers timeouts, KeyboardInterrupt,
            # and bugs in _finalize. A run that aborted before _finalize is
            # always reported FAILED here, never inferred as COMPLETED from
            # an empty or all-succeeded step prefix (#85).
            self._emit_pipeline_end(
                pipeline_result
                or self._finalize(
                    completed_steps, None, run_id, started_at_ts, force_status=RunStatus.FAILED
                ),
                run_id,
                message=abort_message,
            )
            if spinner is not None:
                spinner.stop()
            # Release the sink's run-scoped resources (eager-upload pool, backend
            # connection pool). The pipeline is the last user of the sink for
            # this run, so this fires in the finally to cover both normal
            # completion and any error path that bypasses write_run (issue #57).
            # Skipped when a caller (e.g. batch_run) owns the sink across runs,
            # or when the sink opts out via _close_with_run=False.
            if _owns_sink:
                self._close_sink_quietly(sink)

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
        _owns_sink: bool = True,
    ) -> PipelineResult:
        """Execute steps asynchronously and return a PipelineResult.

        When chain=False, all steps run concurrently via asyncio.gather.
        When chain=True, steps run sequentially (each feeds the next).

        Args:
            sink: Optional sink to write run data to. Sinks with run-scoped
                resources (e.g. ObjectStorageSink) are closed automatically when
                the run finishes (their ``close()`` fires in a ``finally`` block),
                so such a sink is spent afterward — construct a fresh one per run.
                Fire-and-forget sinks like WebhookSink opt out
                (``_close_with_run = False``) and stay open/reusable.
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
        # Preflight runs before the main try/finally. When arun() owns the sink
        # it must still close it on an early preflight/validation failure
        # (issue #57); offload the blocking close to a thread to keep the loop
        # responsive. Share the same teardown helper — no duplicated close.
        try:
            # Reject an invalid config-level tenant before model preflight (which
            # may do network work), so a bad ainvoke(config={"tenant_id": ...})
            # fails fast.
            self._reject_config_tenant(config)
            # Offload blocking preflight (ThreadPoolExecutor + provider discovery)
            # to a thread so the event loop stays free during the preflight window.
            await self._validate_steps_async()
        except BaseException:
            if _owns_sink:
                await asyncio.to_thread(self._close_sink_quietly, sink)
            raise

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
        abort_message: str | None = None
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

                    ctx = _StepContext(run_id=run_id, step_index=i - 1, total_steps=total_steps)
                    prepared = self._build_or_prefail_step(
                        ps,
                        i - 1,
                        completed_steps,
                        prev_assets,
                        step_id=step_ids[i - 1],
                    )
                    step = prepared.step
                    logger.debug(
                        "Executing step %d/%d: %s/%s",
                        i,
                        len(self._steps),
                        ps.provider.name,
                        ps.model,
                    )
                    self._emit_step_start(ctx, step, ps)
                    if spinner is not None:
                        spinner.step_starting(
                            ps.provider.name,
                            ps.model,
                            prompt=step.prompt,
                            step_index=i - 1,
                            total=total_steps,
                        )
                    # Input-resolution prefail handling is shared; the runner call
                    # site only differs in sync vs async provider invocation.
                    if prepared.prefailed:
                        result = self._record_prefailed_step(step, ctx)
                    else:
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
                # Concurrent: use as_completed + cancel on failure when fail_fast.
                # external_inputs is the only input source legal in concurrent mode
                # (input_from / chain force sequential — see has_input_from check
                # earlier); thread it through so multimodal first-step calls work.
                steps_and_models = [
                    (ps, self._build_step(ps, ps.external_inputs)) for ps in self._steps
                ]

                # Check the pipeline timeout BEFORE announcing any step —
                # otherwise a run that never actually starts a step still
                # emits step.started, making an aborted run look like it was
                # underway (#85).
                if pipeline_timeout is not None:
                    elapsed = time.monotonic() - started_at_mono
                    if elapsed >= pipeline_timeout:
                        msg = (
                            f"Pipeline timeout exceeded after {elapsed:.1f}s"
                            f" (limit: {pipeline_timeout}s)"
                        )
                        raise PipelineTimeoutError(msg)

                for idx, (ps, step) in enumerate(steps_and_models):
                    self._emit_step_start(
                        _StepContext(run_id=run_id, step_index=idx, total_steps=total_steps),
                        step,
                        ps,
                    )

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
        except BaseException as exc:
            # Only stash a synthetic abort message when _finalize never ran
            # for this run (pipeline_result still None) — e.g. a mid-loop
            # PipelineTimeoutError or fail-fast cancellation. A
            # PipelineError raised by _maybe_raise_pipeline_error AFTER a
            # normal _finalize already carries accurate step-level errors
            # via error_summary().
            if pipeline_result is None:
                abort_message = sanitize_error(str(exc))
            raise
        finally:
            # Guarantee on_run_end fires — covers timeouts, cancellation,
            # and bugs in _finalize. A run that aborted before _finalize is
            # always reported FAILED here, never inferred as COMPLETED from
            # an empty or all-succeeded step prefix (#85).
            self._emit_pipeline_end(
                pipeline_result
                or self._finalize(
                    completed_steps, None, run_id, started_at_ts, force_status=RunStatus.FAILED
                ),
                run_id,
                message=abort_message,
            )
            if spinner is not None:
                spinner.stop()
            # Release the sink's run-scoped resources (issue #57). close() is
            # blocking (ThreadPoolExecutor.shutdown) — offload to a thread so the
            # event loop stays responsive while it drains. If the surrounding
            # task is cancelled mid-await the close keeps running in its thread
            # but is no longer awaited here; acceptable for cleanup. Skipped when
            # a caller owns the sink (batch) or it opts out (_close_with_run).
            if _owns_sink:
                await asyncio.to_thread(self._close_sink_quietly, sink)

    def _make_failed_step(
        self, ps: _PipelineStep, exc: Exception, *, step_id: str | None = None
    ) -> Step:
        """Create a FAILED step from an unhandled exception.

        ``step_id`` preserves correlation with an already-emitted
        ``step.started`` event (concurrent fail-fast path, #86); omit to
        mint a fresh id.
        """
        from genblaze_core.providers.base import classify_api_error

        # Preserve external_inputs on the failed-step record so the manifest
        # shows what the step was supposed to consume.
        step = self._build_step(ps, ps.external_inputs, step_id=step_id)
        step.status = StepStatus.FAILED
        step.error = sanitize_error(str(exc))
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
        # The already-built Step for each task, carrying the step_id that
        # was announced via step.started. Cancellation/exception placeholders
        # must reuse this id rather than minting a new one, or the later
        # step.failed event won't correlate with its own step.started (#86).
        task_step: dict[asyncio.Task, Step] = {}

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
            task_step[t] = step

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
                    result = self._make_failed_step(task_ps[t], exc, step_id=task_step[t].step_id)

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
                ps_cancelled = task_ps[t]
                step = self._build_step(
                    ps_cancelled, ps_cancelled.external_inputs, step_id=task_step[t].step_id
                )
                step.status = StepStatus.FAILED
                step.error = "Step cancelled due to fail-fast after prior step failure"
                results[idx] = step
            elif t.done():
                try:
                    results[idx] = t.result()
                except Exception as exc:
                    logger.debug("Task %d failed", idx)
                    results[idx] = self._make_failed_step(
                        task_ps[t], exc, step_id=task_step[t].step_id
                    )

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
            sink: Optional sink to write each run to. Shared across all items
                and closed once after the whole batch (not per item), unless the
                sink opts out via ``_close_with_run = False``.
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
        # The batch owns the shared sink across all items: per-item runs must
        # NOT close it (_owns_sink=False), or item 2+ would write to a closed
        # sink. The batch closes it once after the loop (issue #57).
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
                _owns_sink=False,
            )

        results: list[PipelineResult] = []
        try:
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
        finally:
            # finally so a pipeline-level error mid-batch still releases the sink.
            self._close_sink_quietly(sink)
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

        # The batch owns the shared sink: per-item runs must NOT close it
        # (_owns_sink=False). The batch closes it once after gather (issue #57).
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
                    _owns_sink=False,
                )

        if items is not None:
            tasks = [_run(self._apply_item_to_steps(self._steps, item)) for item in items]
        else:
            assert prompts is not None
            tasks = [
                _run([replace(ps, prompt=self._resolve_prompt(ps, p)) for ps in self._steps])
                for p in prompts
            ]
        try:
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
        finally:
            # finally so the re-raise path above still releases the shared sink.
            # Every task has completed before gather returns, so closing once
            # here cannot race a concurrent on_step_complete/write_run.
            await asyncio.to_thread(self._close_sink_quietly, sink)
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

        Raises:
            GenblazeError: If any step uses ``external_inputs=`` — templates
                describe a static pipeline shape and cannot carry runtime
                Asset payloads. Re-add ``external_inputs=`` after
                ``PipelineTemplate.instantiate(...)``.
        """
        from genblaze_core.pipeline.template import PipelineTemplate, StepTemplate

        for i, ps in enumerate(self._steps):
            if ps.external_inputs:
                raise GenblazeError(
                    f"Step {i}: external_inputs= cannot be serialized to a "
                    f"PipelineTemplate (templates describe pipeline shape, not "
                    f"runtime Asset payloads). Build the template without "
                    f"external_inputs=, then re-add them on the instantiated "
                    f"pipeline."
                )

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
