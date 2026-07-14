<!-- created: 2026-04-29 -->
# Ingest sink and non-generative pipelines

**Status:** active · **Owner:** architecture subagent · **Target releases:** `genblaze-core` 0.3.2 · **Shape:** A (additive — no API breaks) · **Feedback refs:** new bug batch 2026-04-29 #5 (`Pipeline`/`ObjectStorageSink` wrong shape for live ingest / UGC / archival / DAM / podcast hosting); P0-04 (Asset passthrough)

## Goal

Generalize the `BaseSink` and `Pipeline` surface so non-generative workflows — live ingest, UGC, archival, DAM, podcast hosting — are first-class instead of forcing fake `SyncProvider` shims. Ship two primitives: `BaseSink.put_asset(asset)` for standalone asset writes (no `Run` wrapper) and `Pipeline.ingest(assets=..., source=...)` for ingest-shaped runs (no `Provider` required). Manifest captures source attribution rather than generation parameters.

**Done when:** a podcast-hosting app can `pipeline.ingest(assets=[Asset(url='https://feed/ep1.mp3')], source='rss')` and get a manifest with full provenance for the import (source URL, ingest timestamp, hash) without writing any `Provider`; a DAM tool can iterate `sink.list()`, hash, write per-asset manifests via `sink.put_asset(..., tenant_id=...)`, and reverse-look up via `sink.read_manifest_for_asset(asset_id, tenant_id=...)`.

## Subagent brief

### Engineering posture

You are an expert open-source SDK engineer with experience in DAM, archival, and live-media systems. The current SDK is generation-shaped; this plan introduces ingest-shaped without a parallel ABC tree. The architectural pivot must be additive — every existing user keeps working. Match `BaseSink` / `Pipeline` idioms; do not fork them.

### Required reading (in order)

1. `AGENTS.md`, `ARCHITECTURE.md`, `CLAUDE.md`
2. `docs/exec-plans/feedback.md` — "P0-04", "P0-05", "P0-06", "Analysis pipeline primitives" cross-cutting initiative
3. `docs/exec-plans/active/p0-p1-feedback-execution.md` Wave 4 — analysis StepTypes (`INGEST` / `IMPORT`); this plan composes with Wave 4
4. `docs/exec-plans/active/storage-backend-hardening-tranche.md` — Plan 1; `BaseSink.put_asset` builds on `KeyBuilder`, `BackendKey`, `Encryption`
5. `libs/core/genblaze_core/storage/sink.py` — `BaseSink`, `ObjectStorageSink`
6. `libs/core/genblaze_core/pipeline/pipeline.py` — `Pipeline`, `Step`, `Run`
7. `libs/core/genblaze_core/models/{run,step,asset}.py` — current data shape
8. `libs/core/genblaze_core/models/enums.py` — `StepType` (gets `INGEST` / `IMPORT` from Wave 4)

### Success bar (review gate)

- **Bugs**: `put_asset` round-trips `sha256` and `media_type` (the C.1 clarification from `multimodal-chat-provider.md`). `Pipeline.ingest` runs through canonical hashing deterministically. Ingest manifests re-verify against themselves.
- **Duplication**: do not add `IngestPipeline` as a separate class — `Pipeline.ingest()` is a factory method on `Pipeline`. Do not add an `AssetSink` ABC — `put_asset` is on `BaseSink`. Do not reinvent `KeyStrategy`; reuse Plan 1's `KeyBuilder`.
- **Performance**: `put_asset` is one backend call. `Pipeline.ingest` for N assets is N parallel `put_asset` calls under existing concurrency primitives.
- **Scalability**: ingest workflows for 1000+ assets work without loading all bytes into memory; manifest size grows linearly (acceptable; `MAX_MANIFEST_BYTES` is the cap; recommend per-batch ingest beyond that).
- **Pattern-fit**: `Pipeline.ingest` produces a `Run` with `StepType.INGEST` steps; manifest schema unchanged (only new step types).

## Phase 1 — `BaseSink.put_asset` (Wk 7)

### D1 — Standalone asset writes

| File | Change |
|------|--------|
| `libs/core/genblaze_core/storage/sink.py` | `BaseSink.put_asset(asset: Asset, *, manifest_uri: str \| None = None, tenant_id: str \| None = None) -> Asset`. Writes asset bytes via backend; returns asset with `url` populated and `sha256`/`media_type` confirmed. `put_assets(list[Asset])` parallel variant. Uses `KeyBuilder` (Plan 1). |
| `libs/core/genblaze_core/storage/sink.py` | `BaseSink.read_manifest_for_asset(asset_id, *, tenant_id) -> Manifest \| None` for tenant-scoped reverse lookup in CONTENT_ADDRESSABLE layouts. |
| `libs/core/tests/unit/test_put_asset.py` | **NEW.** Round-trips `sha256`, `media_type`; cache-stable across calls; CONTENT_ADDRESSABLE dedup; reverse lookup. |

## Phase 2 — `Pipeline.ingest` factory (Wk 8-9, after Wave 4 lands StepType.INGEST)

### D2 — Pipeline.ingest

| File | Change |
|------|--------|
| `libs/core/genblaze_core/pipeline/ingest.py` | **NEW.** `Pipeline.ingest(assets=[...], *, source: str, source_metadata: dict \| None = None, sink=None) -> PipelineResult`. Each asset becomes a `Step(type=StepType.INGEST, provider=None, assets=[asset], metadata={"source": source, **source_metadata})`. Manifest captures attribution. Runs `put_asset` for each. |
| `libs/core/genblaze_core/pipeline/pipeline.py` | `Pipeline.ingest(...)` classmethod thin wrapper around `ingest.py`. |
| `libs/core/genblaze_core/models/step.py` | Allow `Step.provider: str \| None = None` for ingest steps. Validator: provider may be `None` *only* if `step_type ∈ {INGEST, IMPORT}`. |
| `libs/core/tests/unit/test_pipeline_ingest.py` | **NEW.** RSS feed source; UGC upload; bulk DAM import; manifest re-verifies; canonical hash deterministic across permuted asset orders within the same Run. |

## Phase 3 — Docs (Wk 10)

### D4 — Ingest workflows guide

| File | Change |
|------|--------|
| `docs/features/ingest-workflows.md` | **NEW.** Recipes: live ingest (RTMP segments), UGC upload, archival/DAM (bulk import + classify chain), photo library, podcast hosting (download → transcribe → store). |
| `examples/ingest_podcast_episode.py` | **NEW.** End-to-end runnable: fetch episode, store, transcribe (chains into Wave 6 Whisper). |
| `examples/ingest_ugc_upload.py` | **NEW.** User-uploaded asset → `put_asset` → manifest → moderation hook. |

## Cross-plan dependencies

- **Depends on** Plan 1 — `BaseSink.put_asset` uses `KeyBuilder`, `BackendKey`, `Encryption` value types.
- **Depends on** master-plan Wave 4 — `StepType.INGEST` / `IMPORT` lands first.
- **Composes with** master-plan Wave 6 — Whisper provider chains naturally after `Pipeline.ingest`.
- **No dependency on** Plans 2 or 3.

## Acceptance gates

- [ ] `Pipeline.ingest(assets=[...], source="rss")` produces a Run with deterministic canonical hash (golden vector test)
- [ ] `BaseSink.put_asset(asset)` round-trips `sha256`, `media_type`; idempotent under repeated calls
- [ ] Three runnable examples in `examples/ingest_*.py`
- [ ] `make test && make lint && make typecheck` green
- [ ] CHANGELOG: `### Added` (Pipeline.ingest, BaseSink.put_asset)

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| `Step.provider=None` weakens type guarantees | Validator restricts None to `step_type ∈ {INGEST, IMPORT}` only |
| Subagent introduces parallel `IngestPipeline` class | Review gate forbids; must be a `Pipeline.ingest()` factory method |
| Manifest size explodes for 1000+ asset ingest | `MAX_MANIFEST_BYTES` cap; documented; recommend per-batch ingest for large imports |
| Canonical-hash determinism breaks across asset-order permutations | Test asserts hash equality for same asset set in different orders (sort by content — `asset_provenance_key`, not `asset_id` — after the sink populates each asset's hash; see issue #76) |

## Out of scope

- `AssetLibrarySink` — speculative; defer until requested
- DAM search / tagging primitives — user-app territory
- RTMP / HLS-aware live ingest — recipe-level only; SDK provides primitives, not a daemon
