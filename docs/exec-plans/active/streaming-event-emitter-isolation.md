<!-- last_verified: 2026-07-14 -->
# Streaming Event-Emitter Isolation & Lifecycle Correctness

Hardens `Pipeline.stream()`/`astream()` and `AgentLoop.stream()`/`astream()` against a shared
class of bugs: the active stream emitter was mutable, per-instance state, so concurrent streams
on one object cross-delivered events, abandoned workers leaked memory, aborted runs reported false
success, and cancelled concurrent steps lost their event correlation id.

## Problem

- `Pipeline._event_emitter` and `AgentLoop._emitter` are plain instance attributes set by
  `stream()`/`astream()` at the top of the call and cleared/restored on exit. A second concurrent
  `stream()` call on the *same* instance overwrites the first's emitter mid-flight, so the first
  stream's later events (step.progress, step.completed, the terminal event) land in the second
  stream's queue instead of its own (#79 for `AgentLoop`, #84 for `Pipeline`).
- On early break, the sync worker thread keeps running to completion in the background (by
  design — Python threads can't be cancelled), but nothing stopped it from continuing to `put()`
  events onto a queue the caller has already stopped draining. Memory grows proportional to the
  remaining run length (#74).
- `run()`/`arun()`'s exception fallback called `_finalize(completed_steps, ...)` — the same
  helper used for normal completion — which infers `COMPLETED` from `all(s.status == SUCCEEDED
  for s in completed_steps)`. `all([])` is `True`, so a `pipeline_timeout` firing before any step
  started (or any other pre-finalization exception) was reported as a *successful* terminal event
  (#85). The concurrent async path also emitted `step.started` before checking the timeout,
  making an aborted run look like steps were underway.
- Concurrent `arun(fail_fast=True)`: when a sibling step fails, pending tasks are cancelled and
  replaced with placeholder FAILED `Step` objects built via a fresh `_build_step()` call with no
  `step_id` — minting a new UUID instead of reusing the id already announced via that task's own
  `step.started` event (#86).
- `step.completed` / `step.failed` map through `step_complete_to_stream_event(ev, run_id)`, but
  the call site dropped the `run_id` parameter and relied on the emitter's own `run_id` attribute,
  which is never set (the emitter is constructed before the run id exists) (#87).
- Two streaming tests were weak: one ended in a bare `time.sleep` with no assertion, and one
  asserted an absolute wall-clock bound against synthetic `time.sleep` steps — flaky under CI
  scheduler jitter (#48). `AgentLoop.astream()`'s early-break/cancel path had no test at all (#51).

## Non-goals (deliberately excluded)

- Bounding the queue with `maxsize` + blocking `put()`. Closing the emitter on early break already
  stops growth without introducing a producer-side deadlock risk.
- A `RunStatus.CANCELLED` distinction for aborted runs. `FAILED` unambiguously signals "did not
  complete successfully," which is what the acceptance criteria require; introducing a new status
  would also require a new terminal event type, well beyond this batch's scope.
- Locking/guarding against concurrent `stream()` calls with an explicit error. Per-thread/task
  isolation via `contextvars` gives true concurrent support for free, which is a better fit for a
  reusable service object than making concurrent use an error.

## Scope — file touch list

| File | Change |
|---|---|
| `libs/core/genblaze_core/pipeline/streaming.py` | Add `EmitterSlot` (contextvars-backed thread/task-local slot). Fix `QueueEmitter.on_step_complete` to accept an explicit `run_id` override. |
| `libs/core/genblaze_core/pipeline/pipeline.py` | Replace `self._event_emitter` instance attribute with a class-level `EmitterSlot`; `attach_emitter`/`_event_emitter` delegate to it. `stream()`/`astream()` install the emitter inside the worker thread/task (not the generator's own frame) and close it on early break. `_gather_fail_fast`/`_make_failed_step` preserve `step_id` for cancelled/errored concurrent steps. Reorder the concurrent-async timeout check before `step.started` emission. Add `_finalize(..., force_status=...)` and restructure `run()`/`arun()`'s exception path so an aborted run is always finalized `FAILED`, never inferred `COMPLETED`. `_emit_step_complete_event` forwards `run_id`. |
| `libs/core/genblaze_core/agents/loop.py` | Mirror the same `EmitterSlot` fix for `self._emitter`; mirror the early-break `emitter.close()` fix in `stream()`/`astream()`. |
| `libs/core/tests/unit/test_streaming.py` | Harden the two weak tests (#48); add regression tests for concurrent-stream isolation (#79/#84), abandoned-queue growth (#74), aborted-run terminal events (#85), fail-fast step_id preservation (#86), and `run_id` on step terminal events (#87). |
| `libs/core/tests/unit/test_agent_loop.py` | Add `AgentLoop.astream()` normal + early-break/cancel coverage (#51) and a deterministic (`threading.Barrier`-based) regression test for the agent-level emitter race (#79). |
| `docs/features/streaming.md`, `docs/features/pipeline.md`, `docs/features/agents.md` | Document emitter isolation, the closed-on-break backpressure behavior, and the aborted-run terminal-event guarantee. |
| `CHANGELOG.md` | `[Unreleased]` entries under `genblaze-core` for #74, #79/#84, #85, #86, #87. |

## Behavioral guarantees after landing

1. Two concurrent `stream()`/`astream()` calls on the same `Pipeline` or `AgentLoop` instance each
   receive only their own events, with disjoint `run_id` sets and their own terminal event.
2. After an early break, an abandoned sync worker's `put()` calls become no-ops as soon as the
   break is detected — no unbounded growth proportional to remaining run length.
3. `pipeline_timeout` (or any pre-finalization exception) always emits `pipeline.failed`, never
   `pipeline.completed`; the concurrent async path never emits `step.started` for steps that will
   not run.
4. Every `step.failed` from concurrent fail-fast cancellation or a task exception carries the same
   `step_id` as its own earlier `step.started`.
5. `step.completed` / `step.failed` carry `run_id`, matching every other pipeline-scoped variant.
6. `test_stream_early_break_drains_without_daemon_error` and
   `test_stream_early_break_does_not_block` make concrete, deterministic assertions (no bare
   sleeps, no absolute wall-clock thresholds).
7. `AgentLoop.astream()` has coverage for normal iteration and the early-break cancel path.

## Test plan

- TDD per issue: reproduce first (failing test against the pre-fix code, verified via temporary
  `git stash` of only the production files), then fix, then re-verify green.
- `make test` / `make lint` clean; `make typecheck` shows the same single pre-existing
  `media/aac.py` error as `origin/main` (unrelated to this change).
- Known-flaky/pre-existing failures unrelated to this change, confirmed identical on a fully
  reverted checkout: `test_parquet.py` / `test_pipeline_chain_integration.py` (numpy/pyarrow ABI
  mismatch in this dev environment) and several `cli/tests/test_cli.py` cases (same pyarrow issue
  plus a click version drift in `CliRunner.stderr`).
