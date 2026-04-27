<!-- last_verified: 2026-04-27 -->
# Streaming Events Improvements

Closes the gap between the streaming event spec (`libs/spec/schemas/events/v1/`) and what
Python actually emits. Driven by maintainer feedback items 24–31. Improves UX for long-running
generations (heartbeats, ETA, queued state), fixes one documented regression (`StepRetriedEvent`
never reaches the stream), and unblocks production-grade dashboards without per-app workarounds.

## Problem

- **`StepRetriedEvent` never reaches stream consumers.** `_emit_retry()` fires the
  `on_retry` callback, but `Pipeline._install_progress_tracer()` only wraps `on_progress`.
  Users running with `RetryPolicy` see silence between attempts. Schema shipped in 0.3.2;
  Python side missed the wiring.
- **`request_id` (provider prediction id) only surfaces once.** Available on the `on_submit`
  callback, never on `step.progress` / `step.completed` / `step.failed`. UIs can't show debug
  info live; users grep manifests after the fact.
- **No ETA on `step.started`.** Consumers building progress UIs hard-code per-model duration
  knowledge, or render meaningless spinners. Today the SDK already has `elapsed_sec`; missing
  half is the duration target.
- **Long-poll silence.** When `_adaptive_poll_interval` stretches past 15s on multi-minute
  generations (Sora, Veo, Stable Audio long-form), the stream produces no events. Load
  balancers and SSE proxies time out idle connections; users see "stuck."
- **No queued state.** Sequential and capacity-bound concurrent steps are invisible until
  they `step.started`. UIs can't render an "Up next" tray. Spec already implies this state
  (`step.started` description: "transitions from queued to running") but no event fires.
- **`preview_url` is spec-only.** Schema field declared in `step.progress`; zero connectors
  populate it. A still-frame preview every 30s of a 5-minute video render is one of the
  cheapest UX wins available.

## Non-goals (deliberately excluded)

- **Per-model `expected_duration_sec` registry.** 200+ hand-coded entries across 11 connectors,
  rotting silently as providers update infrastructure. Stale data → progress bars pegging at
  0.99 — worse than no progress bar. Replaced with an opt-in kwarg on `.step()` (caller-supplied,
  caller-owns-accuracy).
- **Synthesized `progress_pct` from elapsed/expected.** Worse-than-honest UX — pegs at 0.99
  and sticks. Expose the inputs (`elapsed_sec`, `expected_duration_sec`) and let consumers
  compute if they want.
- **`phase: Literal[...]` field on `StepProgressEvent`.** `data.status` already conveys
  "submitted"/"running" today. Without the sink/upload phase wired, `phase` would just be
  two names for the same information. Defer until sink-upload progress events ship.
- **`upstream_id` as a top-level `Step` field.** Would change `canonical_hash` for every run
  (AGENTS.md invariant: hashes must remain deterministic). Goes in `step.metadata` instead;
  manifest serialization keeps `metadata` opaque.
- **`step.cancelled` event for fail-fast cancelled-while-queued.** Document that
  `pipeline.failed` is terminal for any still-queued step. Adding a new event type adds
  surface area for one edge case.
- **Removing `on_submit` callback.** Crash-recovery contract; orthogonal to streaming.

## Scope — file touch list

### PR1 — Wire `StepRetriedEvent` to the stream (the documented bug)

| File | Change |
|---|---|
| `libs/core/genblaze_core/pipeline/streaming.py` | Add `QueueEmitter.on_retry(ev)` — `put(ev)` directly; `StepRetriedEvent` is already a `StreamEvent`. |
| `libs/core/genblaze_core/pipeline/pipeline.py` | In `_install_progress_tracer`, also wrap `on_retry` so the composite forwards to user callback + `self._emit_event(ev)`. |
| `libs/core/tests/unit/test_streaming.py` | New `test_step_retried_event_reaches_stream()` — flaky provider, assert `StepRetriedEvent` lands in the queue between `step.started` and `step.completed`. |
| `CHANGELOG.md` | Note the fix under Unreleased. |

### PR2 — `request_id` on progress / completed / failed

| File | Change |
|---|---|
| `libs/core/genblaze_core/providers/base.py` | In `_attempt_once` / `_attempt_once_async`, after `submit()` returns, set `step.metadata["upstream_id"] = str(prediction_id)` (alongside the existing `on_submit` callback fire). |
| `libs/core/genblaze_core/providers/progress.py` | Add `request_id: str \| None = None` to `ProgressEvent`. |
| `libs/core/genblaze_core/providers/base.py` | `_fire_progress` reads `step.metadata.get("upstream_id")` and passes it as `request_id`. |
| `libs/core/genblaze_core/observability/events.py` | Add `request_id: str \| None = None` to `StepProgressEvent`, `StepCompletedEvent`, `StepFailedEvent`. |
| `libs/core/genblaze_core/pipeline/streaming.py` | `progress_to_stream_event` and `step_complete_to_stream_event` propagate `request_id`. |
| `libs/spec/schemas/events/v1/step-progress.schema.json` | Add `request_id` (optional string). |
| `libs/spec/schemas/events/v1/step-completed.schema.json` | Add `request_id` (optional string). |
| `libs/spec/schemas/events/v1/step-failed.schema.json` | Add `request_id` (optional string). |
| `libs/spec/ts/genblaze.d.ts` | Regenerate via `make ts-types`. |
| `libs/core/tests/unit/test_streaming.py` | Assert `request_id` propagates from a fake provider's `submit()` return value to `step.progress` events. |
| `libs/core/tests/unit/test_spec_conformance.py` | Add the new field to the conformance fixtures. |

### PR3 — `expected_duration_sec` on `step.started`

| File | Change |
|---|---|
| `libs/core/genblaze_core/pipeline/pipeline.py` | Add optional `expected_duration_sec: float \| None = None` kwarg to `Pipeline.step()`. Stash on `_PipelineStep`. Pass to `_emit_step_start`. |
| `libs/core/genblaze_core/observability/events.py` | Add `expected_duration_sec: float \| None = None` to `StepStartedEvent`. |
| `libs/spec/schemas/events/v1/step-started.schema.json` | Add `expected_duration_sec`. |
| `libs/spec/ts/genblaze.d.ts` | Regenerate. |
| `libs/core/tests/unit/test_streaming.py` | Assert the kwarg flows through to the event. |
| `libs/core/tests/unit/test_spec_conformance.py` | Add to fixtures. |
| `docs/features/streaming.md` | Document `expected_duration_sec` as caller-supplied, used for ETAs. Note that consumers can compute `progress_pct = elapsed_sec / expected_duration_sec` clamped to `[0, 0.99]` if they want a synthesized bar. |

### PR4 — Heartbeat events on long polls

| File | Change |
|---|---|
| `libs/core/genblaze_core/providers/progress.py` | Add `is_heartbeat: bool = False` to `ProgressEvent`. |
| `libs/core/genblaze_core/observability/events.py` | Add `is_heartbeat: bool = False` to `StepProgressEvent`. |
| `libs/core/genblaze_core/providers/base.py` | New constants: `_HEARTBEAT_THRESHOLD_SEC = 15.0`, `_HEARTBEAT_CHUNK_SEC = 10.0`. Helper `_sleep_with_heartbeats(interval, step, config, start_time)` and async twin — when `interval >= 15s`, sleep in `≤10s` chunks, fire `_fire_progress(..., is_heartbeat=True)` between chunks. Replace the bare `time.sleep(interval)` / `await asyncio.sleep(interval)` in poll loops. Skip when emitter is disabled (no work to do). |
| `libs/core/genblaze_core/pipeline/pipeline.py` | Add `heartbeats: bool = True` parameter to `stream()` / `astream()`. When `False`, set a flag the queue emitter consults to drop `is_heartbeat=True` events. (Cheapest path: filter at `_emit_event`.) |
| `libs/core/genblaze_core/pipeline/streaming.py` | `progress_to_stream_event` propagates `is_heartbeat`. |
| `libs/spec/schemas/events/v1/step-progress.schema.json` | Add `is_heartbeat`. |
| `libs/connectors/langsmith/genblaze_langsmith/tracer.py` | Filter `is_heartbeat=True` events in `on_event` (don't bill cycles for keepalive ticks). Add unit test. |
| `libs/spec/ts/genblaze.d.ts` | Regenerate. |
| `libs/core/tests/unit/test_streaming.py` | Test heartbeat fires when interval ≥ 15s, doesn't fire when < 15s, suppressed when `heartbeats=False`. |
| `libs/core/tests/unit/test_provider_retry.py` | Confirm heartbeat doesn't interfere with retry timing. |

### PR5 — Additive `StepQueuedEvent`

| File | Change |
|---|---|
| `libs/core/genblaze_core/observability/events.py` | New `StepQueuedEvent(type="step.queued", run_id, step_id, step_index, total_steps, provider, model, reason: Literal["serial","concurrency_limit"])`. Add to `AnyStreamEvent` union and `StreamEventType` literal. |
| `libs/spec/schemas/events/v1/step-queued.schema.json` | New schema file mirroring the Pydantic class. |
| `libs/spec/schemas/events/v1/stream-event.schema.json` | Add `step.queued` to the discriminator enum and `oneOf`. |
| `libs/core/genblaze_core/pipeline/streaming.py` | `QueueEmitter.on_queued(ev)` (just `put(ev)`). |
| `libs/core/genblaze_core/pipeline/pipeline.py` | **Sequential path:** at top of step `i`'s iteration, emit `step.queued(reason="serial")` for steps `i+1..n` (only on the first iteration to avoid re-emit). **Concurrent path:** in `_sem_execute`, if `sem.locked()` at entry, emit `step.queued(reason="concurrency_limit")` for the current step; existing `step.started` upfront emission stays put. |
| `libs/spec/ts/genblaze.d.ts` | Regenerate. |
| `libs/core/tests/unit/test_streaming.py` | Sequential: queued events for upcoming steps. Concurrent + max_concurrency=1: queued events for blocked steps. Fail-fast cancellation: queued-without-started doesn't crash drain; `pipeline.failed` is the terminal. |
| `libs/core/tests/unit/test_spec_conformance.py` | Add `step.queued` fixture. |
| `docs/features/streaming.md` | Document the additive semantics: `step.started` continues to fire on dispatch; `step.queued` is a new optional signal. |

### PR6 — `preview_url` for Runway + Luma + contract docs

| File | Change |
|---|---|
| `libs/connectors/runway/genblaze_runway/provider.py` | In `poll()` (or wherever progress is fired), surface `task.thumbnail` / equivalent preview field from the Runway response into `_fire_progress(..., preview_url=...)`. Validate via `validate_asset_url`. |
| `libs/connectors/luma/genblaze_luma/provider.py` | Same — surface intermediate frame URL when the Luma generation has progress images. |
| `docs/features/streaming.md` | New section: "Populating preview_url" — connector authors call `_fire_progress(..., preview_url=...)` whenever the upstream API exposes a draft frame / thumbnail / waveform. Explicitly list which connectors do (Runway, Luma) and which don't (everyone else, until requested). |
| `libs/connectors/runway/tests/`, `libs/connectors/luma/tests/` | Mock a poll response with the preview field; assert `preview_url` flows through. |

## Test plan

- `make test` after each PR — all must pass before merge (AGENTS.md invariant).
- `make ts-types` after every schema change; commit the regenerated `genblaze.d.ts`.
- `test_spec_conformance.py` validates Pydantic ↔ JSON Schema parity — must pass.
- New end-to-end test in `libs/core/tests/unit/test_streaming.py`:
  `test_full_event_surface_for_long_running_step` — flaky provider with adaptive poll, asserts
  the stream contains: started, queued (sibling), progress (with request_id), heartbeats,
  retried, completed (with request_id). One test, exercises PR1–PR5 together.

## Risks

| Risk | Mitigation |
|---|---|
| Heartbeat events flood LangSmith / OTel exporters at scale | `is_heartbeat` flag plumbed through; LangSmith tracer filters in PR4; users disable via `Pipeline.stream(heartbeats=False)`. |
| Per-call `expected_duration_sec` is wrong → bad ETA | Caller-owned; the SDK echoes verbatim. Document that consumers should clamp to `[0, 0.99]` or render a confidence band. |
| `preview_url` is provider-specific and may break SSRF policy | Connectors validate with `validate_asset_url` before forwarding (existing helper, HTTPS-only). |
| `step.queued` fail-fast cancelled-without-started orphan | Documented as expected: `pipeline.failed` is the terminal for any still-queued step. Add a test that confirms the drain loop doesn't hang. |
| Schema changes break TS consumers | All field additions are optional. Discriminated union grows by one variant (`step.queued`); TS consumers that switch on `event.type` get a `never` branch — caught at compile time. |

## Sequencing

PR1 first (the documented bug). PR2–PR5 are independent and can land in any order;
recommend the listed sequence because PR2 (`request_id`) is the most-asked-for feature
and PR4 (heartbeats) is the most subtle. PR6 lands per-connector after the core
work settles.
