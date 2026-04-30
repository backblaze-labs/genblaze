<!-- created: 2026-04-29 -->
# Storage backend hardening

**Status:** active · **Owner:** storage subagent · **Target releases:** `genblaze-s3` 0.3.0, `genblaze-core` 0.3.0 · **Shape:** B (re-cut ABC; deprecation window) + F (P0 bugs) + A (new primitives) · **Feedback refs:** new bug batch 2026-04-29 (10 bugs); P1-19, P2-04, P2-05, P2-19, P2-34

## Goal

Re-cut the `StorageBackend` ABC into a production-grade surface: typed config, error parity with `ProviderError`, sync + async, tracer-instrumented, with every P0 bug resolved by architectural rework (not patches) and the missing primitives shipped as first-class methods.

**Done when:** every P0 bug below has a regression test that fails on `genblaze-s3 0.2.x` and passes on 0.3.0; the conformance suite covers `S3StorageBackend` end-to-end (S3 path via `moto`, B2 path via staging creds when present, otherwise skip); `StorageError` carries `request_id`/`status_code`/`is_retriable`/`operation`/`error_code`; `StorageConfig` exposes pool/retry/multipart/timeout knobs; every backend operation emits tracer events; sync and async surfaces have method-by-method parity.

## Subagent brief

### Engineering posture

You are an expert open-source SDK engineer. The codebase is a 14-package Python monorepo with strict invariants (Pydantic v2 only, JSON-schema-authoritative wire, canonical hash determinism). Long-term architectural quality is the goal — never hotfixes. Match the existing idioms; do not introduce new patterns where existing ones fit.

### Required reading (in order)

1. `AGENTS.md` — invariants, doc map
2. `ARCHITECTURE.md` — components, data flows, canonical files
3. `CLAUDE.md` (project root) and `~/.claude/CLAUDE.md` — engineering working agreement
4. `docs/exec-plans/feedback.md` — P1-19, P2-04, P2-05, P2-19, P2-34 + Source log entry dated 2026-04-29
5. `docs/features/object-storage.md` — current sink / backend story
6. `libs/core/genblaze_core/storage/base.py` — current abstract `StorageBackend`
7. `libs/core/genblaze_core/storage/sink.py` — `BaseSink`, `ObjectStorageSink`
8. `libs/connectors/s3/genblaze_s3/backend.py` — `S3StorageBackend` impl (every P0 bug lives here)
9. `libs/core/genblaze_core/exceptions.py` — `ProviderError` (target shape for `StorageError`)
10. `libs/core/genblaze_core/observability/tracer.py` — Tracer ABC pattern to mirror
11. `libs/core/genblaze_core/_utils.py` — existing `jittered_backoff`, `check_ssrf`, `_SECRET_PATTERNS`

### Scope

- **Backends: B2 (`for_backblaze`) and AWS S3 (generic constructor) only.** Cloudflare R2 works implicitly via the generic S3 path; no R2-specific code or tests. **No MinIO.** Do not add MinIO-specific code paths, MinIO Docker test targets, or MinIO documentation. R2 / MinIO compatibility claims in the README stay; engineering effort focuses on B2 and S3.
- **Tests:** S3 via `moto` (in-process), B2 via staging creds (`B2_KEY_ID_STAGING` / `B2_APP_KEY_STAGING` env). When staging creds absent, B2 conformance tests `pytest.skip` with a reason, not fail.

### Success bar (review gate before each phase ships)

Run this audit against the diff before requesting review:

- **Bugs**: every code path explicitly handles `None`, empty input, network errors, concurrent writes, partial multipart upload failures, `botocore.ClientError` subclasses with non-200 statuses. Negative tests exist for each.
- **Duplication**: grep before adding any helper. Did `_utils.py` already have it? Did `BaseProvider`'s retry-phase wiring already solve it? Did `ObjectStorageSink` already have it?
- **Performance**: identify any per-call network round-trip that does not have to be one. Identify any unbounded memory paths (whole-object reads, unpaginated lists, list-into-RAM). Identify any N×M loops.
- **Scalability**: does this work at 10× connections, 10× object size, 10× concurrent ops? Are pool size, multipart threshold, list pagination all tunable? Default to backpressure-friendly behavior (paginated, streamed).
- **Pattern-fit**: Pydantic v2 for data classes; lazy import for optional deps; tracer hook on every backend method; error parity with `ProviderError`. No bespoke patterns.

If any audit point fails, fix it before shipping the phase.

## Problem

Ten production-grade bugs (security, correctness, performance) and twelve missing primitives that block real-world apps (cleanup, audit, GC, browser uploads, async, observability). Patching individually means 10+ small PRs against a brittle surface. Re-cutting the ABC and impl together gives one coherent shape — and lands the bugs as side effects of a proper architecture, not as hotfixes.

### Confirmed P0 bugs (architectural fix, not patch)

| # | Bug | Architectural fix |
|---|-----|-------------------|
| 1 | `put()` returns presigned URL embedding access-key-id (credential leak if persisted to logs/DB) | `put() -> BackendKey`. Presigned URLs are opt-in via separate `presigned_get/put/post` methods returning a `PresignedURL` value object with redaction-safe `__repr__` (signature replaced with `***`). |
| 2 | `get_url(expires_in=...)` silently ignored under `public_url_base` (paid-feed leak) | New `URLPolicy(prefer="public"\|"presigned"\|"auto")` arg; `auto` returns public when `public_url_base` set, presigned otherwise. Conflict (`expires_in` set with `prefer="public"`) raises `URLPolicyError`. No silent precedence. |
| 3 | SSE-C asymmetric (works on `put`, missing on `get/copy`) | Symmetric `Encryption` value object accepted by `put/get/copy/head` uniformly; missing customer key on `get` of an encrypted object raises typed `EncryptionRequiredError`. |
| 4 | `for_backblaze(auto_lifecycle=True)` mutates lifecycle on construction; 403 only logs warning | `auto_lifecycle=False` default; explicit `apply_lifecycle()` method for opt-in. Preflight 403 raises `BackendAuthError`. `preflight=False` skips entirely (offline tests / placeholder creds). |
| 5 | `KeyStrategy.HIERARCHICAL` with `prefix="runs"` → `runs/runs/...` | Pure `KeyBuilder` class; strategies own layout; prefix normalized at strategy boundary (strip leading/trailing slashes, dedupe path segments at the seam). |
| 6 | `ObjectLockConfig(mode="WAT")` silently accepted | `mode: Literal["GOVERNANCE","COMPLIANCE"]` Pydantic-validated at construction; runtime `ValueError` on invalid value. |
| 7 | `get_url()` does HeadBucket round-trip before signing | sigv4 is local crypto. Drop the HeadBucket. Cache region at construction (or via lazy preflight) and reuse. |
| 8 | Umbrella `__getattr__` walks into `pyarrow` on `from genblaze import ParquetSink` | (Owned by Plan 5 — umbrella package fix.) |
| 9 | `genblaze.__version__` ≠ pip metadata ≠ user-agent | (Owned by Plan 5 — version unification.) |
| 10 | README R2/MinIO quickstart uses `access_key_id` but `__init__` requires `aws_access_key_id` → TypeError | Accept `access_key_id` as kwarg alias of `aws_access_key_id` (additive, no deprecation needed). README fix in Plan 5. |

### Missing primitives

`list / head / delete_many / delete_prefix / get_range / stream / presigned_get / presigned_put / presigned_post / progress callbacks / multipart-config tuning / async parity / per-put Object Lock`.

### Production-readiness gaps

- `StorageError` is structurally bare (no `request_id` / `status_code` / `is_retriable` / `operation`).
- Connection pool, retries, multipart thresholds hardcoded as module privates.
- Tracer not wired into backend calls — no per-op latency or request-id without monkey-patching boto3.

## Architecture (locked decisions)

1. **`StorageBackend` ABC re-cut to a single sync + async surface.** Every method has a sync and async pair (`get`/`aget`, `put`/`aput`, `list`/`alist`, …). Default async impl threadpool-wraps the sync impl. Native async via `aioboto3` (lazy import; optional extra `genblaze-s3[async]`) for hot paths only (`get`, `put`, `stream`). No second ABC.

2. **`StorageError` parity with `ProviderError`.** Same shape: `error_code: StorageErrorCode`, `request_id: str | None`, `status_code: int | None`, `is_retriable: bool`, `operation: str`. `RETRYABLE_STORAGE_CODES` shared by retry classification.

3. **`StorageConfig` dataclass replaces module privates.** Frozen dataclass: `max_pool_connections`, `connect_timeout`, `read_timeout`, `multipart_threshold`, `multipart_chunk_size`, `retries`, `user_agent_extra`, `signing_addressing_style`. Defaults preserve current behavior.

4. **`KeyBuilder` is a pure value-object class.** No I/O, no state. Strategies (`HIERARCHICAL`, `CONTENT_ADDRESSABLE`, custom) are functions on `KeyBuilder` instances. Idempotent under repeated calls. Path normalization at the boundary fixes the prefix-dup bug (#5).

5. **Tracer instrumentation.** `traced(op_name)` decorator wraps every backend method; emits `storage.{op}` spans with `key`, `bucket`, `request_id` attributes. Composes with existing `Tracer` ABC.

## Phased delivery

### Phase 0 — Foundation (Wk 1)

| File | Change |
|------|--------|
| `libs/core/genblaze_core/storage/config.py` | **NEW.** `StorageConfig` frozen dataclass (8 knobs). |
| `libs/core/genblaze_core/storage/errors.py` | **NEW.** `StorageError(GenblazeError)` aligned with `ProviderError`; `StorageErrorCode` enum; `RETRYABLE_STORAGE_CODES`; `classify_botocore_error()` helper. |
| `libs/core/genblaze_core/storage/key_builder.py` | **NEW.** `KeyBuilder` pure class + `KeyStrategy` callables; prefix normalization. |
| `libs/core/genblaze_core/storage/_tracer.py` | **NEW.** `traced(op_name)` decorator for sync + async paths. |
| `libs/core/genblaze_core/storage/base.py` | Re-cut ABC with full sync + async method set (see Phase 2 table). Default async impls threadpool-wrap sync. |
| `libs/core/genblaze_core/__init__.py` | Re-export `StorageConfig`, `StorageError`, `StorageErrorCode`, `KeyBuilder`, `URLPolicy`, `Encryption`. |
| `libs/connectors/s3/tests/conformance/test_storage_backend.py` | **NEW.** Parametric over `S3StorageBackend` (moto-mocked S3) and B2 (staging, `pytest.skip` if no creds). Asserts sync + async parity, tracer hooks fire, error shape matches. |

### Phase 1 — P0 bug architecture (Wk 2 - Wk 2.5)

| File | Change |
|------|--------|
| `libs/connectors/s3/genblaze_s3/url_policy.py` | **NEW.** `URLPolicy` + `URLPolicyError`. (bug #2) |
| `libs/connectors/s3/genblaze_s3/encryption.py` | **NEW.** `Encryption` value object (SSE-C / SSE-KMS / SSE-S3). (bug #3) |
| `libs/connectors/s3/genblaze_s3/presigned.py` | **NEW.** `PresignedURL` value object with redaction-safe `__repr__`; `presigned_get/put/post` methods. (bug #1) |
| `libs/connectors/s3/genblaze_s3/backend.py` | (a) `put()` returns `BackendKey`, not presigned URL. (b) Drop HeadBucket from `get_url`; reuse cached region. (c) `auto_lifecycle=False` default; preflight 403 raises. (d) Accept `access_key_id` alias. (e) `KeyBuilder` integration replaces inline path concatenation. |
| `libs/core/genblaze_core/models/object_lock.py` | `ObjectLockConfig.mode: Literal["GOVERNANCE", "COMPLIANCE"]` Pydantic-validated. (bug #6) |
| `libs/connectors/s3/tests/regression/test_p0_bugs_2026_04_29.py` | **NEW.** One test per bug, named after the bug; each fails on 0.2.x via git checkout, passes on 0.3.0. |

### Phase 2 — Missing primitives (Wk 3)

| Method | Surface | Notes |
|--------|---------|-------|
| `list(prefix, *, max_keys=1000, continuation_token=None) -> ListPage` | sync + async | `ListPage(entries: list[FileEntry], next_token: str \| None)`; `FileEntry(key, size, last_modified, etag, content_type, storage_class)`. |
| `head(key) -> ObjectMetadata \| None` | sync + async | `None` for missing key; never raises on 404. |
| `delete_many(keys, *, dry_run=False) -> DeleteResult` | sync + async | Batched via S3 DeleteObjects. `dry_run` returns key list without deleting. |
| `delete_prefix(prefix, *, dry_run=True) -> DeleteResult` | sync + async | **`dry_run=True` default** — caller must explicitly opt out. Walks `list()` pages, batches deletes. |
| `get_range(key, *, offset, length) -> bytes` | sync + async | HTTP `Range:` header. |
| `stream(key, *, chunk_size=8MiB)` | sync (`Iterator[bytes]`) + async (`AsyncIterator[bytes]`) | Lazy chunked download. |
| `presigned_get/put(...)` | sync + async | Returns `PresignedURL`. **`presigned_post` deferred** — its boto3 wire shape (`{"url", "fields"}` POST policy) doesn't fit `PresignedURL` and needs a separate `PresignedPost` value object with similar redaction. Tracked as a follow-up sub-phase; not a tranche acceptance gate. |
| `progress` callback | added to `put`, `get`, `stream` | `progress: Callable[[TransferProgress], None] \| None`. Emits `storage.progress` event when set. |
| `Encryption` accepted on `put/get/copy/head` | sync + async | Closes SSE-C asymmetry. |
| `ObjectLock` per-put | sync + async | Per-put Object Lock; defaults to bucket-level config. |

### Phase 3 — Async parity (Wk 3.5 - Wk 4)

| File | Change |
|------|--------|
| `libs/connectors/s3/genblaze_s3/async_backend.py` | **NEW.** Async impls using `aioboto3` (lazy import; falls back to threadpool wrap with INFO log). |
| `libs/connectors/s3/genblaze_s3/__init__.py` | Re-export `AsyncS3StorageBackend` (or expose via `S3StorageBackend.async_client()`). |
| `libs/connectors/s3/tests/conformance/test_async_parity.py` | **NEW.** Every sync method has a matching async test in the same suite. |
| `libs/connectors/s3/pyproject.toml` | Add `[project.optional-dependencies].async = ["aioboto3>=12,<13"]`. |

## Cross-plan dependencies

- **Subsumes** master-plan `p0-p1-feedback-execution.md` Wave 2A item P1-19 (range/stream). P1-12 (B2 env aliases), P1-14 (LocalFilesystemSink), P1-20 (`SyncProvider.emit_bytes`) stay in Wave 2A and inherit the hardened backend.
- **Provides foundation for** Plan 4 (`BaseSink.put_asset` uses `KeyBuilder`, `BackendKey`, `Encryption`).
- **Consumes** Plan 5's `_version` helper for default user-agent base.
- **No dependency on** Plan 2 or Plan 3.

## Acceptance gates

- [ ] `make test && make lint && make typecheck` green
- [ ] All 10 P0 bug regression tests pass on 0.3.0; verified to fail on 0.2.x via `git checkout v0.2.8`
- [ ] Conformance suite (S3 via moto, B2 via staging) covers every backend method, sync + async parity
- [ ] Tracer events emitted for every backend method (assertion in conformance suite)
- [ ] `StorageError` round-trips `request_id`, `status_code`, `is_retriable`, `operation`, `error_code`
- [ ] `StorageConfig` documented in `docs/features/object-storage.md`
- [ ] CHANGELOG: `### Added` (new methods, async surface, `StorageConfig`), `### Changed` (`put()` return shape with deprecation note), `### Fixed` (10 P0 bugs listed), `### Security` (credential-leak fixes called out separately)
- [ ] Migration guide `docs/migrations/storage-0.2-to-0.3.md` covers `put()` return shape, `auto_lifecycle` default flip, `URLPolicy` introduction

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| `put()` return shape change breaks callers using the presigned URL | Deprecation window: 0.3.0 supports `S3StorageBackend(legacy_put_returns_presigned=True)` for one minor; removed 0.4.0. CHANGELOG `### Security` callout. |
| `KeyBuilder` re-cut breaks existing keys in B2 buckets | Golden-vector test: 100 known `(run_id, asset_id, prefix)` tuples produce byte-identical keys to 0.2.x except for the prefix-dup bug case (which is the fix). |
| Async surface doubles maintenance | Default async = threadpool wrap of sync. Native `aioboto3` only for `get`/`put`/`stream`. |
| `dry_run=True` default on `delete_prefix` surprises users | Documented; loud INFO log on first call; aligns with Stripe/AWS CLI conventions. |
| `aioboto3` is heavy install path | Optional extra (`genblaze-s3[async]`); minimal install unaffected. |
| Subagent over-engineers the ABC re-cut | Phase 0 review gate: file count ≤8, LOC ≤600 added, no new abstractions beyond `StorageConfig`/`StorageError`/`KeyBuilder`/`URLPolicy`/`Encryption`/`traced` decorator. |

## Out of scope (intentional)

- **MinIO** — no MinIO code paths, no MinIO Docker tests, no MinIO docs. R2 implicit via generic S3 path; not separately tested.
- **B2 management surface** (application-key, lifecycle CRUD beyond `apply_lifecycle()`, replication, legal-hold, CORS). Track in future `b2-management-surface.md`.
- **`BaseSink.put_asset` / `Pipeline.ingest`** — Plan 4 owns the sink layer.
- **C2PA assertions** — Plan 2 follow-up.
- **Pricing data → YAML** — deferred (no incident evidence).
