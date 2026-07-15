<!-- last_verified: 2026-07-15 -->
# Pipeline Core Correctness — Emitter Scope, Step-Field Routing, Batch Concurrency, Template Params

Four independent `Pipeline`/`PipelineTemplate` correctness bugs found during pre-release review,
bundled into one branch because three of the four touch the same file (`pipeline.py`) and should
land as a single coherent diff rather than three competing edits to the same regions.

## Problems

**#151 — class-level `EmitterSlot` leaks nested-pipeline events into an outer `stream()`.**
`Pipeline._emitter_slot` is a `ClassVar[EmitterSlot]` — one `contextvars.ContextVar` shared by
every `Pipeline` instance in the process. `EmitterSlot` correctly isolates concurrent
`stream()`/`astream()` calls *on the same instance* (#147, ContextVar is per thread/task Context),
but it does nothing to isolate *different instances* running in the *same* Context. A step
provider, moderation hook, or callback that constructs and runs a distinct `inner_pipeline.run()`
synchronously inside `outer.stream()`'s worker thread (or an `await inner.arun()` from a child task
inheriting the worker's context) reads the *same* ContextVar via `inner._event_emitter`, so
`inner`'s `pipeline.started`/`step.*`/terminal events land on `outer`'s queue.

**#53 — `Pipeline.step()` swallows `metadata=`/`prompt_visibility=` into provider params.**
`Step` already has first-class `metadata: dict` and `prompt_visibility: PromptVisibility` fields,
but `step()` has no matching kwargs, so both are silently absorbed into `**extra_params` →
`Step.params`. `prompt_visibility` is privacy-sensitive (controls whether the prompt is persisted
cleartext in the manifest); silently defaulting every step to `PUBLIC` regardless of what the
caller passed is a data-exposure footgun.

**#83 — Batch APIs don't enforce `max_concurrency` consistently.** Sync `batch_run()` accepts and
documents `max_concurrency` but the implementation is a plain `for` loop — the argument is dead.
`abatch_run()` does use it (`asyncio.Semaphore(max_concurrency)`) but never validates it, so
`max_concurrency=0` builds a semaphore no task can ever acquire — an unconditional hang.

**#52 — `PipelineTemplate.instantiate()` renders `prompt` but not `params`.** `variables=` is
threaded through `PromptTemplate(...).render()` for `StepTemplate.prompt` only; `StepTemplate.params`
is passed through via `**st.params` completely unrendered, so a template with
`params={"voice": "{locale}_voice"}` reaches the provider with the literal `"{locale}_voice"`
string instead of the substituted value.

## Non-goals (deliberately excluded)

- **Real OS-thread concurrency for sync `batch_run()`.** Rejected after analysis, not an oversight —
  see "Decision: batch_run stays sequential" below.
- **A general Pipeline-level metadata *builder* surface beyond a single `.metadata(**kwargs)` call.**
  The issue's acceptance criteria asks for "a documented fluent/run API" — one additive method
  wired into `RunBuilder.meta()` satisfies that without inventing a second metadata mechanism.
- **Feature-flagging the #52 params-rendering fix.** Considered and rejected as over-engineering:
  rendering only fires when the caller already opts in via `variables=` (identical gate to the
  existing `prompt` rendering), so there is no new default-on behavior to stage-roll — the
  "flag" already exists in the form of the `variables=` argument itself.

## Revisions after red-team review

A red-team pass (skeptical senior-engineer review of this plan, before any code was written)
found five real gaps in the first draft, now folded into the plan below:

1. **`__copy__` must NOT share `_emitter_slot`.** The first draft mirrored the WARN-dedup lock's
   sharing behavior for shallow `copy.copy()` clones (used by `batch_run`/`abatch_run`). That
   reintroduces #151's exact symptom in a narrower shape: a clone executing inside the *same*
   thread/task Context as the pipeline that spawned it (e.g. `batch_run()` invoked from a hook
   running inside `outer.stream()`'s worker) would read `outer`'s emitter through the shared
   `ContextVar` and leak its events into `outer`'s queue. Unlike the lock/dedup-set, there is no
   existing use case requiring clones to share the emitter — `attach_emitter` is for `AgentLoop`'s
   explicit, opt-in composition, not implicit batch-clone sharing. `__copy__` now rebuilds a fresh
   `EmitterSlot` for the clone, matching `__getstate__`/`__setstate__`'s existing "give the clone
   independent unpicklable state" treatment.
2. **`AgentLoop._emitter_slot` (`agents/loop.py:107`) has the identical `ClassVar[EmitterSlot]`
   defect** — not just a similar one. Deferring it to a tech-debt bullet would ship a PR whose
   entire premise is "we found and fixed this class of bug" right next to a known, mechanically
   identical instance of the same bug. `AgentLoop` has no `__copy__`/`__getstate__`/`__setstate__`
   today, so the fix is the same one-line change (`ClassVar` → instance attribute built in
   `__init__`) with no copy-protocol follow-up needed. Folded into this PR's #151 commit.
3. **`batch_run()`'s `UserWarning` must not fire on the historical default.** `max_concurrency`
   defaults to `5` and no existing call site overrides it, so warning whenever `max_concurrency !=
   1` would fire on virtually every call in the codebase and test suite — exactly the "noisy,
   ignorable warning" anti-pattern. Fixed by using the same `None`-sentinel convention this file
   already uses for `raise_on_failure` (`_resolve_raise_on_failure`): `batch_run`'s signature
   becomes `max_concurrency: int | None = None`; `None` silently resolves to `5` (today's
   behavior, unchanged for unwary callers); any explicit value (including literally `5`) is a
   deliberate ask for concurrency the sync path can't provide, and warns once, pointing at
   `abatch_run()`. `abatch_run()` keeps its plain `int = 5` signature since it genuinely honors
   the value — only `>= 1` validation is added there.
4. **`_apply_item_to_steps()` (`batch_run(items=...)`) is a second, unguarded route into
   `Step.params`.** It merges arbitrary `item` dict keys straight into `params`, so
   `batch_run(items=[{"metadata": {...}}])` or `{"prompt_visibility": ...}` would still smuggle
   both fields past #53's fix. Extended to pop `metadata`/`prompt_visibility` into the
   corresponding `Step` fields (same merge/override semantics as `step()`) and to reject the
   `inputs`/`input` reserved names, matching `step()`'s guard.
5. **Metadata key collisions raise instead of silently favoring internal graph keys.** The first
   draft let `_fallback_models`/`_input_from` silently clobber caller metadata on collision. That's
   inconsistent with this same file's existing reserved-name philosophy (`step()`'s `inputs`/
   `input` guard raises rather than swallowing). `step()` now rejects `metadata={"_fallback_models":
   ...}` / `metadata={"_input_from": ...}` outright, at call time, same location as the
   `inputs`/`input` check.

## Decision: `batch_run()` stays sequential — validated, not parallelized

The issue's suggested fix offers two options: implement real concurrency, or validate + document
that sync stays sequential. Real `ThreadPoolExecutor`-based concurrency was evaluated and
rejected:

- `batch_run()` clones share `_PipelineStep.provider` instances (`copy.copy(self)` is shallow, and
  `_apply_item_to_steps`/`dataclasses.replace` never clone the provider). Provider adapters are not
  documented or tested as thread-safe — `genblaze_core.mocks.MockProvider.call_count += 1` and
  connectors' `poll_progress()` caches (`self._progress_cache[key] = job`, per `docs/features/streaming.md`)
  are plain unguarded instance state. Real OS-thread preemption (unlike asyncio's cooperative
  single-thread concurrency, which is the only concurrency model this SDK's provider contract has
  ever been validated against) can interleave a compound `+=` mid-bytecode and lose updates —
  a concrete, demonstrable regression risk to every existing and future provider, not a theoretical one.
- The shared `sink` (`ObjectStorageSink`, `ParquetSink`, ...) is written from a single sequential
  loop today; multi-threaded `sink.write_run()` calls are unaudited for concurrent-write safety.
- `abatch_run()` already provides genuine concurrency via `asyncio` — the correct, tested, documented
  path for parallel batch execution. Sync `batch_run()`'s `max_concurrency` becomes a validated,
  honestly-documented no-op with a runtime `UserWarning` (once per call, suppressible by passing
  `max_concurrency=1`) pointing callers at `abatch_run()`.

This satisfies the issue's acceptance criteria ("...either runs concurrently or raises / warns that
sync batch concurrency is unsupported") via the warn branch, and is the smaller, lower-risk change.

## Scope — file touch list

| File | Change |
|---|---|
| `libs/core/genblaze_core/pipeline/pipeline.py` | #151: `_emitter_slot` moves from `ClassVar[EmitterSlot]` to an instance attribute built in `__init__`; `__copy__`/`__getstate__`/`__setstate__` all rebuild a fresh `EmitterSlot` for the clone (never shared — ContextVar is also unpicklable). #53: `step()` gains `metadata=`/`prompt_visibility=` kwargs; `_PipelineStep` carries both; reserved-name guard extended to reject `metadata`/`prompt_visibility` smuggled through `params={}` AND to reject caller metadata keys colliding with internal graph keys (`_fallback_models`/`_input_from`); `_build_step()` merges caller metadata with internal graph metadata; fallback-model retry (`_try_fallback_models` sync + inlined async path) now `.update()`s metadata instead of replacing it wholesale, so caller metadata and `_input_from` survive a fallback retry; new `Pipeline.metadata(**kwargs)` fluent method (additive, mirrors `RunBuilder.meta()`) wired into `_finalize()`. `_apply_item_to_steps()` (batch `items=`) extended to route `metadata`/`prompt_visibility` keys to the right `Step` fields instead of `params`, and to reject `inputs`/`input`, closing the same privacy gap via the batch-items entry point. #83: `batch_run()`'s `max_concurrency` becomes `int | None = None` (`None` silently resolves to `5`, unchanged default behavior); any explicit value validates `>= 1` and emits a one-time `UserWarning` pointing at `abatch_run()` for genuine concurrency; `abatch_run()` keeps `int = 5` and adds `>= 1` validation only (no behavior change, it already honors the value). | 
| `libs/core/genblaze_core/agents/loop.py` | #151 (folded in per red-team finding): identical `ClassVar[EmitterSlot]` → instance attribute in `__init__`. No copy-protocol follow-up needed (no `__copy__`/`__getstate__`/`__setstate__` exist on `AgentLoop`). |
| `libs/core/genblaze_core/pipeline/template.py` | #52: new `_render_template_value()` helper (recursive dict/list/tuple walk, `PromptTemplate.render()` on string leaves, mirroring `_reject_credentials_in_params`'s walk shape) applied to `StepTemplate.params` in `instantiate()`, same as `prompt`. |
| `libs/core/tests/unit/test_streaming.py` | #151: (a) distinct-inner-`Pipeline`-instance-inside-outer-`stream()` regression test (inner's events must not reach outer's queue), sync + async; (b) a `batch_run()`-clone-inside-outer-`stream()` regression test, proving the `__copy__` fix; (c) an `AgentLoop`-composition smoke test confirming `attach_emitter` still works after the `ClassVar`→instance-attribute change. |
| `libs/core/tests/unit/test_pipeline_preflight.py` or new `test_pipeline_step_fields.py` | #151 copy/pickle coverage for the now-instance-level `_emitter_slot` (shallow copy, deepcopy, pickle **all** rebuild independent slots — no sharing case, unlike the WARN lock) — mirrors the existing `_warned_preflight_lock` tests structurally but with different assertions. #53 tests for `metadata=`/`prompt_visibility=` landing on `Step`, reserved-name rejection (both via `step()` kwargs-in-params and via `batch_run(items=...)`), metadata-collision raising, fallback-retry metadata preservation, `Pipeline.metadata()`. |
| `libs/core/tests/unit/test_pipeline.py` or `test_pipeline_batch_raise.py` | #83 tests: `max_concurrency=0`/negative raise for both APIs; explicit `batch_run(max_concurrency=5)` warns; implicit default (`max_concurrency` omitted) does not warn; `abatch_run` never warns. |
| `libs/core/tests/unit/test_pipeline_template.py` | #52 tests: top-level string param rendering, nested dict/list params, non-string params untouched, missing-variable error parity with prompt rendering, literal-doubled-brace params surviving unchanged. |
| `docs/features/pipeline.md`, `docs/features/pipeline-templates.md`, `docs/features/streaming.md`, `docs/features/agents.md` | Document `metadata=`/`prompt_visibility=`/`Pipeline.metadata()`, the batch concurrency contract, per-instance emitter scoping (Pipeline + AgentLoop), and template param rendering. |
| `CHANGELOG.md` | `[Unreleased]` bullets under `### genblaze-core` for #151 (incl. the `AgentLoop` parity fix), #53, #83, #52 (flagged as a behavior change for existing template JSON with literal identifier-shaped `{...}` params combined with `variables=`). |

## Test plan

- TDD per issue: failing test first against pre-fix code, then the minimal fix, then green.
- `make test` / `make lint` clean. Known pre-existing/unrelated failure: `test_pipeline_chain_integration.py::test_chain_pipeline_to_parquet_sink` (numpy/pyarrow ABI mismatch in this dev env, confirmed on a clean `origin/main` checkout before any change here).
- Panel review (3 independent `Agent` sub-agents) before push: issue-coverage/correctness,
  concurrency+privacy, engineering quality/DX. Triangulate P0s and ≥2-reviewer P1s, fix, re-verify.

## Panel review outcome

Three independent reviewers (issue-coverage/correctness, concurrency+privacy, engineering
quality/DX) reviewed the full working-tree diff cold, each fetching all four issues independently.
No P0s. Two genuine P1 bugs surfaced (both fixed, both now covered by a regression test that fails
on a manual revert):

1. **`_build_input_resolution_failure_step()` didn't set `prompt_visibility=ps.prompt_visibility`**
   (concurrency/privacy reviewer) — a step pre-failed by an invalid `input_from` reference silently
   reverted to `PromptVisibility.PUBLIC` regardless of what the caller passed, even though the
   failed `Step` still carries the cleartext `prompt` — defeating the exact privacy guarantee #53
   set out to fix for that one code path. Fixed by adding the missing kwarg; regression test
   `test_input_from_failure_preserves_prompt_visibility`.
2. **`_apply_item_to_steps()` (the `batch_run(items=...)` entry point) never applied the
   `_RESERVED_GRAPH_METADATA_KEYS` collision guard** `step()` enforces (concurrency/privacy
   reviewer, P1; engineering-quality reviewer, P2 — same finding, triangulated) — a batch item could
   forge `_fallback_models`/`_input_from` values straight into `Step.metadata` even on a step with
   no configured `fallback_models`/`input_from`, corrupting the internal replay-data invariant the
   guard exists to protect. Fixed by applying the same guard; regression test
   `test_batch_run_items_rejects_metadata_colliding_with_reserved_graph_keys`.

One test-coverage gap was raised independently by two reviewers (issue-coverage and engineering
quality) — missing async coverage for the `_execute_step_async` fallback-metadata `.update()` fix —
triangulated as blocking per the ≥2-reviewer rule even though the underlying code was already
correct; added `test_fallback_retry_preserves_caller_metadata_async`.

Minor P2 nits from the engineering-quality reviewer also applied: extracted the duplicated
`("inputs", "input")` reserved-name tuple into a shared `_RESERVED_INPUT_PARAM_NAMES` constant used
by both `step()` and `_apply_item_to_steps()`; fixed a docstring typo; gave `Pipeline.__copy__`
clones an independent `_run_metadata` dict (sub-threshold finding, ~40 confidence, but a one-line
fix consistent with the rest of this PR's "clones get independent mutable state" philosophy).
Re-ran `make test`/`make lint` after every fix — all green (same single pre-existing
`test_pipeline_chain_integration.py` failure, unrelated).
