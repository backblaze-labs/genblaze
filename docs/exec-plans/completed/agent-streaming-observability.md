<!-- last_verified: 2026-04-24 -->
# Agent Loop, Streaming, and Observability

Close three gaps called out by the framework review: no reasoning loop, no push-style streaming, and minimal tracing.

> **Update (0.2.3):** The `StreamEvent` design described below ("tagged union dataclass") shipped in its original form, then migrated to a Pydantic **discriminated union** with per-variant subclasses. Current authoritative references: [`docs/features/streaming.md`](../../features/streaming.md), `libs/core/genblaze_core/observability/events.py`, and the JSON Schemas at `libs/spec/schemas/events/v1/`. Historical field names below may differ from the current contract — notably: agent events are now flat (no `data` dict), and `step.failed` carries `error` instead of `message`.

## Motivation

| Gap | Current state | Target |
|-----|---------------|--------|
| Reasoning layer | Users chain steps manually; no built-in evaluate→retry | `AgentLoop` + pluggable `Evaluator` with lineage via existing `from_result()` |
| Streaming | `on_progress` / `on_step_complete` callbacks only (pull from caller) | `Pipeline.stream()` / `astream()` yield `StreamEvent` iterators |
| Observability | `StructuredLogger` + `StepSpan` OTel bridge | `Tracer` ABC with NoOp / Logging / OTel / Composite backends + `genblaze-langsmith` connector |

Built in-house — no LangChain/LangGraph import — to keep `genblaze-core` zero-dependency and consistent with existing `Runnable`/`BaseProvider` abstractions.

## Scope

### 1. Streaming

- New `genblaze_core.observability.events.StreamEvent` — tagged union dataclass with fields `type`, `run_id`, `step_id`, `step_index`, `total_steps`, `timestamp`, `progress_pct`, `message`, `data`, `result`
- Event types: `pipeline.started`, `step.started`, `step.progress`, `step.completed`, `step.failed`, `pipeline.completed`, `pipeline.failed`
- `Pipeline.stream(**run_kwargs) -> Iterator[StreamEvent]` — runs pipeline in a worker thread, yields from `queue.Queue`
- `Pipeline.astream(**run_kwargs) -> AsyncIterator[StreamEvent]` — schedules `arun()` as task, yields from `asyncio.Queue`
- `_StreamEmitter` wires existing `on_progress`/`on_step_complete` callbacks to the queue so no provider-side changes are needed
- `ProgressEvent.preview_url` field added — opt-in for providers that expose intermediate frames/waveforms

### 2. Tracer

- `genblaze_core.observability.tracer.Tracer` ABC: `on_run_start`, `on_step_start`, `on_event`, `on_step_end`, `on_run_end`
- `NoOpTracer` (default), `LoggingTracer` (wraps `StructuredLogger`), `OTelTracer` (owns run + step spans), `CompositeTracer` (fan-out)
- `Pipeline(tracer=...)` constructor arg. `structured_log=True` keeps working — internally resolves to `LoggingTracer`
- Tracer errors are swallowed + warned (never break pipeline)
- `StepSpan` kept as-is for provider-level timing; run/step spans owned by tracer are additive

### 3. Agent Loop

- New `genblaze_core.agents` module
- `Evaluator` ABC: `evaluate(result) -> EvaluationResult`, `aevaluate()`
- `EvaluationResult(score: float | None, passed: bool, feedback: str | None, metadata: dict)`
- `CallableEvaluator(fn)` and `ThresholdEvaluator(score_fn, threshold)` helpers
- `AgentLoop(pipeline_factory, evaluator, *, max_iterations, quality_threshold, tracer)` with `run()`, `arun()`, `stream()`, `astream()`
- `pipeline_factory: Callable[[AgentContext], Pipeline]` where `AgentContext` carries prior iterations + last evaluation — the cleanest way to express "build a refined pipeline given prior feedback"
- `AgentResult(iterations: list[AgentIteration], final: PipelineResult, passed: bool, total_cost_usd: float)`
- Every iteration linked via `Pipeline.from_result(prev)` — provenance preserved automatically

### 4. LangSmith connector

- New package `libs/connectors/langsmith/` following the existing connector pattern
- `genblaze_langsmith.LangSmithTracer(api_key=..., project_name=...)` implements `Tracer`
- Lazy imports `langsmith` SDK — installable via `pip install genblaze-langsmith`

## Implementation Order

1. `StreamEvent` dataclass + internal emitter
2. `Tracer` ABC + backends (NoOp, Logging, OTel, Composite)
3. Pipeline integration — `stream()`, `astream()`, `tracer` arg; legacy callbacks keep working
4. `ProgressEvent.preview_url` field
5. `agents/` module
6. `libs/connectors/langsmith/`
7. Tests for each new module
8. Docs + ARCHITECTURE update + Makefile targets

## Risks & Mitigations

- **Sync `stream()` with event loop semantics** — runs pipeline in thread with `queue.Queue`; avoids nested `asyncio.run()`.
- **Tracer exceptions** — caught + logged as warnings; pipelines never break because a backend failed.
- **Agent loop infinite retry** — hard `max_iterations` cap; `quality_threshold=None` means "just run once and evaluate".
- **Evaluator cost** — evaluator can itself be a genblaze pipeline; cost accounting rolls up into `AgentResult.total_cost_usd`.
- **Canonical hash invariant** — no manifest model changes; iteration lineage uses existing `parent_run_id` (already excluded from hash).
- **Backward compat** — `structured_log=True`, `on_progress`, `on_step_complete` all preserved.

## Exit Criteria

- `make test` green for the three new test files plus the langsmith connector tests
- `make lint` clean
- Feature docs created + ARCHITECTURE.md updated
- Examples added under `examples/`
