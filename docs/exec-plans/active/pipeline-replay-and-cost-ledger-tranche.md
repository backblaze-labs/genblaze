<!-- created: 2026-04-29 -->
# Pipeline replay, idempotency, and cost ledger

**Status:** active · **Owner:** pipeline subagent · **Target releases:** `genblaze-core` 0.3.0 (Phase 0 cost; TS-breaking schema bump), 0.3.1 (replay/idempotency/per-step) · **Shape:** A + B (cost summary on `PipelineCompletedEvent` is a TS-affecting schema bump) · **Feedback refs:** P2-16, P2-36, F-2026-04-25-02; `retry-policy-unification.md` "Out of Phase 2" follow-ups

## Goal

`Pipeline.run` lifecycle gains four production-grade primitives: end-to-end cost accounting (with cap), programmatic replay from `run_id`, pipeline-level idempotency for resume/rehydration, and per-step `retry_policy` / `timeout_sec` composition. CLI `genblaze replay` and SDK `Pipeline.replay` share a single code path.

**Done when:** `Pipeline.run(max_cost_usd=...)` aborts a run *before* the next step when cumulative cost exceeds the cap; `PipelineCompletedEvent.cost_summary` rolls up per-step + provider + model cost; `Pipeline.replay(run_id)` and `Pipeline.from_manifest_uri(uri)` produce a re-runnable pipeline; `Pipeline.run(idempotency_key=...)` short-circuits to a cached `PipelineResult` distinguishable from a real run; `.step(retry_policy=..., timeout_sec=...)` overrides provider defaults; the P2-36 chain-input zero-cost bug is closed.

## Subagent brief

### Engineering posture

You are an expert open-source SDK engineer working on `Pipeline.run` lifecycle — load-bearing surface across all 11 connectors. Backward compatibility is mandatory; every change is additive or has a documented deprecation window. Schema bumps are coordinated atomically with TS type regen.

### Required reading (in order)

1. `AGENTS.md`, `ARCHITECTURE.md`, `CLAUDE.md`
2. `docs/exec-plans/feedback.md` — search "P2-16", "P2-36", "F-2026-04-25-02"
3. `docs/exec-plans/active/retry-policy-unification.md` — `RetryPolicy` class (already shipped 0.2.7), Phase 2 "Out of scope" notes (`Pipeline.run(retry_policy=...)`)
4. `docs/exec-plans/active/p0-p1-feedback-execution.md` — Wave 3A/3B context, deprecation helper Wave 0.3
5. `libs/core/genblaze_core/pipeline/pipeline.py` — `.run()`, `.step()`, `.cache()`, `from_result()`, fluent surface
6. `libs/core/genblaze_core/pipeline/cache.py` — `StepCache` + `step_cache_key`
7. `libs/core/genblaze_core/providers/pricing.py` lines 70-79 — broken `per_input_chars` chain-input fall-through
8. `libs/core/genblaze_core/providers/retry.py` — `RetryPolicy` (already shipped)
9. `libs/core/genblaze_core/observability/events.py` — `PipelineCompletedEvent`
10. `libs/spec/schemas/events/v1/pipeline-completed.schema.json` — wire schema (will be bumped)
11. `cli/genblaze_cli/` — current `replay` command (search for `replay` in `main.py` or its module)
12. `libs/core/genblaze_core/storage/sink.py` — `ObjectStorageSink.read_manifest` (shipped 0.2.7)

### Success bar (review gate)

- **Bugs**: cost cap fires *before* next step starts (not after — that wastes the upstream call). Replay handles missing assets gracefully. Idempotency-key collision across structurally different pipelines emits WARNING, not silently returns the wrong result.
- **Duplication**: CLI `replay` and SDK `Pipeline.replay` MUST share `genblaze_core.replay` module — no two implementations. Cost rollup MUST reuse existing `PricingStrategy`; do not reimplement.
- **Performance**: cost cap check is O(1) per step (running sum). Replay loads manifest once. Idempotency-key lookup is one backend call.
- **Scalability**: idempotency cache stored in active sink (file-based, survives process restart). Replay works on runs with 100+ steps without loading all asset bytes into memory.
- **Pattern-fit**: per-step kwargs follow existing `.step(...)` fluent surface; no new builder class introduced.

## Phase 0 — Cost ledger lands first (Wk 5-6) [TS-breaking schema bump]

### B5 — Cost ledger + cap + cost_summary event + P2-36 fix

| File | Change |
|------|--------|
| `libs/core/genblaze_core/pipeline/cost.py` | **NEW.** `CostLedger` instance attached to `PipelineResult`. `add_step(step)`; `total_usd`; `breakdown_by_provider()`; `breakdown_by_model()`. |
| `libs/core/genblaze_core/observability/events.py` | `PipelineCompletedEvent` gains `cost_summary: CostSummary \| None`. `CostSummary(total_usd, by_provider, by_model, currency, was_capped, was_cache_hit)`. |
| `libs/spec/schemas/events/v1/pipeline-completed.schema.json` | Bump schema: add optional `cost_summary` block. Coordinated regen of `libs/spec/ts/genblaze.d.ts` via `make ts-types`. `@genblaze/spec` minor bump. |
| `libs/core/genblaze_core/pipeline/pipeline.py` | `Pipeline.run(max_cost_usd=...)`. Check before each step submit; raise `BudgetExceededError(remaining_usd, would_have_cost_usd)` and return partial `PipelineResult` with `was_capped=True`. |
| `libs/core/genblaze_core/providers/pricing.py:74` | Fix P2-36: `per_input_chars` falls back to `ctx.step.inputs[0].metadata.get("char_count")` when `step.prompt` is empty; if both absent, emit `logger.warning(...)` so the silent zero is visible. |
| `libs/core/tests/unit/test_cost_ledger.py` | **NEW.** Per-step accounting; provider/model breakdown; budget cap before-not-after (mock the next-step submit and verify it's never called); partial result on cap; P2-36 chain-input fall-through coverage. |

## Phase 1 — Replay (Wk 7) [shared CLI/SDK module]

### B2 — Pipeline.replay / from_manifest_uri

| File | Change |
|------|--------|
| `libs/core/genblaze_core/replay.py` | **NEW.** Shared module: `replay_from_manifest(manifest, *, sink=None) -> Pipeline`; `replay_from_run_id(run_id, sink) -> Pipeline`; `replay_from_uri(uri) -> Pipeline`. Reads via `ObjectStorageSink.read_manifest` (already shipped). |
| `libs/core/genblaze_core/pipeline/pipeline.py` | `Pipeline.replay(run_id, *, sink=None)` and `Pipeline.from_manifest_uri(uri)` thin wrappers around the shared module. |
| `cli/genblaze_cli/replay.py` | Refactor existing CLI `replay` to call `genblaze_core.replay.replay_from_run_id` — no logic duplication. |
| `libs/core/tests/unit/test_replay.py` | **NEW.** SDK and CLI replay produce byte-identical Pipeline objects from the same `run_id`. |

## Phase 2 — Per-step composition (Wk 7)

### B3 — per-step retry_policy + timeout_sec

| File | Change |
|------|--------|
| `libs/core/genblaze_core/pipeline/pipeline.py` | `.step(retry_policy=None, timeout_sec=None, ...)`. Precedence: step > pipeline > provider > default. Documented in `.step()` docstring + `docs/features/pipeline.md`. |
| `libs/core/tests/unit/test_per_step_overrides.py` | **NEW.** retry_policy override per step; timeout_sec per step; precedence chain; additive shape (no deprecation needed). |

## Phase 3 — Idempotency (Wk 7) [lands AFTER B5]

### B1 — Pipeline.run(idempotency_key=...)

| File | Change |
|------|--------|
| `libs/core/genblaze_core/pipeline/idempotency.py` | **NEW.** `IdempotencyStore` ABC; `SinkIdempotencyStore(sink)` writes `idempotency/{key}.json` containing `{result_uri, pipeline_signature_hash, stored_at}`. `pipeline_signature_hash` is a *cheap* hash of step metadata (provider class names, model ids, modality) — NOT a canonical pipeline hash. Used only for divergence WARNING, not for keying. |
| `libs/core/genblaze_core/pipeline/pipeline.py` | `Pipeline.run(idempotency_key=None)`. Lookup-first. On hit: load `result_uri` via replay module, return `PipelineResult` with `cost_summary.was_cache_hit=True`. On miss: run normally, store result, return. WARNING when stored `pipeline_signature_hash` differs. |
| `libs/core/tests/unit/test_idempotency.py` | **NEW.** Same key + same pipeline → cache hit; same key + different pipeline → WARNING; cache hit's `cost_summary.was_cache_hit=True`; file-based store survives process restart (subprocess test). |

## Cross-plan dependencies

- **Subsumes** `retry-policy-unification.md` "Out of Phase 2" follow-up (`Pipeline.run(retry_policy=...)` and per-step `retry_policy`).
- **Depends on** master-plan Wave 0.3 (deprecation helper) for any deprecation warnings.
- **No dependency on** Plan 1 (storage), Plan 2 (signing), Plan 4 (ingest).

## Acceptance gates

- [ ] `make test && make lint && make typecheck` green per phase
- [ ] Cost cap aborts before next step submit (verified by mocking next-step submit and asserting it's never called)
- [ ] CLI `genblaze replay` and SDK `Pipeline.replay(run_id)` produce byte-identical Pipeline objects
- [ ] `cost_summary.was_cache_hit` distinguishes cached results from real-run results
- [ ] `make ts-types` regenerated; `@genblaze/spec` 0.4.0 bumped (additive minor)
- [ ] CHANGELOG: `### Added` (cost ledger, replay, per-step overrides, idempotency), `### Fixed` (P2-36)

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| `cost_summary` TS-breaking for strict consumers | Optional field → JSON-schema-additive minor; documented in CHANGELOG and migration note |
| Idempotency cache becomes stale | Each entry has `stored_at`; eviction is caller-managed (SDK doesn't auto-evict); documented |
| Replay loads massive manifests into RAM | `read_manifest` already enforces `MAX_MANIFEST_BYTES` cap |
| CLI/SDK replay drift | Test asserts both code paths produce equal Pipeline objects from same `run_id` |
| `BudgetExceededError` is undefined behavior on partial step | Cap is checked *before* submit, never mid-step; partial result has `was_capped=True` and the failed step is the one that would have started |

## Out of scope

- DAG-aware scheduler (own design doc; deferred from earlier plans)
- Pricing data → YAML extraction (deferred; no incident evidence)
- Multi-tenant cost-by-tenant breakdown (defer until requested)
- C2PA / signing — Plan 2
