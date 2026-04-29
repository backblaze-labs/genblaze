<!-- created: 2026-04-24 -->
# P0 + P1 Feedback Execution Plan

Deliver every P0 and P1 row in `docs/exec-plans/feedback.md` (5 + 20 = 25 items) as a
coherent sequence of tight PRs, with shared foundations built once instead of duplicated
across features.

## Goals & success criteria

- **Clean** — one conformance test suite, one optional-dep isolation pattern, one sink
  emission primitive, one deprecation helper. Avoid N-way duplication where one primitive
  suffices.
- **Scalable** — new providers / sinks / example slugs integrate without re-running every
  decision. Conformance tests grow for free; minimal-install CI catches new leaks as they
  land.
- **Performant** — no regressions on `make test` wall-clock; large-file paths (P1-19) drop
  RAM usage from O(file) to O(chunk); batch_run sync parity closes a 500-track footgun;
  embedding records hashes **once each** never twice.
- **Optimized for review** — each PR is ≤ ~500 LOC (target), has a single theme, and
  doesn't require coordinating state across more than one module.

**Done when:** `feedback.md` shows 0 open P0 rows, 0 open P1 rows, and the Resolved
section grows to ~40+. `tech-debt-tracker.md` absorbs any residuals with concrete scope.

## Dependency map

```
FOUNDATION (Wave 0)
  ├── Cross-provider conformance suite
  ├── Minimal-install CI job
  └── Deprecation helper + policy

QUICK FIXES (Wave 1)                    [parallel w/ each other, blocks nothing]
  P0-03 ← conformance suite
  P1-01, P1-02, P1-04, P1-08, P1-09, P1-11, P1-16

STORAGE/SINK REFACTOR (Wave 2)
  P1-21 ─┐
  P1-12 ─┼→ BaseSink.emit_bytes → P1-14 (LocalFilesystemSink)
         │                       → P1-20 (SyncProvider.emit_bytes)
         │                       → P1-15 (save_manifest)
         └→ StorageBackend.get_range/stream → P1-19

PIPELINE SEMANTICS (Wave 3)             [parallel w/ Wave 2, independent surface]
  P0-01 + P2-33 (lineage) ← deprecation helper
  P1-03 (cache= alias)
  P1-10 (modality default) ← deprecation helper

ANALYSIS PRIMITIVES (Wave 4)            [biggest lift — own PR cycle]
  P0-05 + P0-06 → Step.output, Asset.text, StepType additions, AnalysisProvider
  P0-04 (Pipeline.input) ← analysis StepTypes so ingest is first-class

PROVENANCE CORRECTNESS (Wave 5)         [design decision required first]
  P1-17 → Asset.sha256_embedded + sidecar-default policy

PROVIDER ADDITIONS (Wave 6)
  P1-06 (Gemini image) ← no deps
  P1-05 (Whisper STT)  ← Wave 4 (AnalysisProvider)

FIRST-30-MINUTES POLISH (Wave 7)
  P1-13 (CLI release + README quickstart)
  P1-18 (docs cliff → inline recipes + ship examples/ in wheel)
```

Critical path: Wave 0 → Wave 4 → Wave 6 (Whisper) is the long pole. Waves 1, 2, 3 can run
in parallel once Wave 0 lands.

## Wave 0 — Foundation

Zero feature work. Everything downstream relies on these three pieces existing.

### 0.1 Cross-provider conformance test suite

**File:** `libs/core/tests/unit/test_provider_conformance.py`
**Pattern:** parametrize over every `BaseProvider` subclass discovered via
`entry_points(group="genblaze.providers")` (or a hand-maintained list if we haven't wired
entry points yet).

Assertions (start small, grow with each wave):

```python
@pytest.mark.parametrize("cls", _ALL_PROVIDER_CLASSES)
def test_accepts_models_kwarg(cls):
    """Drives P0-03. Every BaseProvider subclass must accept `models=`."""
    sig = inspect.signature(cls.__init__)
    assert "models" in sig.parameters, f"{cls.__name__} drops models= kwarg"

@pytest.mark.parametrize("cls", _ALL_PROVIDER_CLASSES)
def test_get_capabilities_shape(cls):
    caps = cls(api_key="dummy").get_capabilities()
    assert isinstance(caps, ProviderCapabilities)
    assert caps.supported_modalities  # non-empty

@pytest.mark.parametrize("cls", _ALL_PROVIDER_CLASSES)
def test_has_pricing_strategy(cls):
    reg = cls.models_default()
    for model_id in reg.known():
        assert reg.get(model_id).pricing is not None, f"{model_id} has no pricing"
```

**Expands in later waves with:**
- 5xx retry honors `RETRYABLE_ERROR_CODES` (Wave 1 polish)
- `prepare_payload()` round-trips (Wave 2)
- `emit_progress()` is callable (Wave 2)
- `@dataclass` guard tripwire (P2-35, deferred)

### 0.2 Minimal-install CI smoke test

**File:** `.github/workflows/ci.yml` new job `minimal-install-smoke`.

```yaml
minimal-install-smoke:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
    - run: pip install ./libs/core  # no extras, no connectors
    - run: python -c "
        import genblaze_core
        from genblaze_core import (
          Pipeline, BaseProvider, SyncProvider, BaseSink, Asset, Manifest,
          ModelRegistry, ModelSpec, RunnableConfig, ObjectStorageSink,
        )
        from genblaze_core.mock import MockProvider, MockVideoProvider, MockAudioProvider
        print('OK')
      "
```

Fails fast if any public symbol gains a hidden optional-dep requirement. Drives P1-01,
P1-16, P3-18, and anything similar downstream.

### 0.3 Deprecation helper + written policy

**File:** `libs/core/genblaze_core/_deprecation.py` — small utility module.

```python
def deprecated(*, until: str, replacement: str | None = None) -> Callable:
    """Decorator. Wraps `warnings.warn(DeprecationWarning, stacklevel=2)`.
    Emits `<name>: deprecated; will be removed in <until>. Use <replacement>.`
    Records the deprecation in `_DEPRECATED_SYMBOLS` for changelog tooling."""
```

**Policy doc:** `docs/features/deprecation-policy.md` + entry in `AGENTS.md`.
- Breaking changes ship with one minor-version deprecation window minimum.
- CHANGELOG `[x.y.z]` section adds a `### Deprecated` group.
- The 0.4.0 release note lists every 0.3.x deprecation being removed.

Addresses feedback P3-05. Prereq for P0-01 and P1-10.

## Wave 1 — Quick fixes (parallel, independent, tiny)

Each item is ≤ 100 LOC. Batch as 3–4 parallel PRs.

| # | ID | Change | Test |
|---|----|--------|------|
| 1.1 | **P0-03** | `GMICloudBase.__init__` adds `models: ModelRegistry \| None = None` + `super().__init__(models=models, ...)`. | Conformance suite (0.1) catches any future regression across every provider. |
| 1.2 | **P1-01** | Create `libs/core/genblaze_core/mock.py` containing `MockProvider`, `MockVideoProvider`, `MockAudioProvider` — no pytest dependency. `testing.py` re-imports them and keeps its pytest-only `ProviderComplianceTests` fixture. Update `genblaze_core.__all__`. | Minimal-install smoke (0.2). |
| 1.3 | **P1-16** | Lazy-import `urllib3` inside the transfer function that needs it (currently `storage/transfer.py:14`). Also lazy-import `boto3`-adjacent imports in the same file. | Minimal-install smoke (0.2). |
| 1.4 | **P1-02** | `PromptTemplate` gains `model_validator(mode="before")` that accepts a single positional string: `PromptTemplate("hello {name}")` → `PromptTemplate(template="hello {name}")`. | New test `test_prompt_template_positional.py`; un-break shipped `examples/batch_with_templates.py`. |
| 1.5 | **P1-04** | `batch_run()` sync path: replace the serial `for` loop at `pipeline.py:1362` with `ThreadPoolExecutor(max_workers=max_concurrency)`. Fail loudly if `max_concurrency < 1`. Preserve output order. | Parametrized test: 10 runs, `max_concurrency=4`, assert wall time ≤ ~⅓ of serial equivalent. |
| 1.6 | **P1-08** | `ModerationHook.check_prompt` runs against resolved `input_from` text payloads in addition to `step.prompt`. Emit an audit entry per moderated source. | `test_moderation_hook_input_from.py` — UGC routed via `input_from=[0]` triggers the hook. |
| 1.7 | **P1-09** | In `DalleProvider` allowed-roots resolver: `Path(root).resolve()` on both the caller-provided path and the allowlist entries so Darwin `/tmp` ↔ `/private/var/folders` symlinks round-trip. | Parametrized test over macOS temp paths. |
| 1.8 | **P1-11** | `FFmpegTransform`: run `ffmpeg -filters` once at init, cache in `_available_filters`. `overlay_text` raises at build time with "libfreetype not available" if `drawtext` filter missing. Add ops: `trim`, `extract_audio`, `concat`, `split`, `atempo`, `replace_audio`, audio mixdown (`amix`), multi-track (`amerge`). Each op is ~15 LOC. | One test per new op (golden-file via `ffprobe` on output). |

**Expected LOC:** ~600 total across 8 items. **Expected PR count:** 4 (group 1.1+1.2+1.3
as "optional-dep isolation + P0-03 conformance"; 1.4 solo; 1.5+1.8 as "pipeline +
ffmpeg polish"; 1.6+1.7 solo).

## Wave 2 — Storage / sink refactor

One tight cluster. Best as **two PRs**: (A) sink primitives, (B) sink consumers.

### 2A — Sink primitives + storage-layer additions

Covers: **P1-12, P1-14, P1-19, P1-20, P1-21.**

**Design:**

1. **P1-21 — Configurable user-agent base on `S3StorageBackend`.**
   Add `user_agent_base: str | None = None` kwarg. Default to `b2ai-genblaze/<version>`
   when unset. `user_agent_extra=` already appends. Document both in the connector README
   + AGENTS.md.

2. **P1-12 — B2 env-var aliasing.** In `S3StorageBackend.for_backblaze()`:
   ```python
   key_id = key_id or os.getenv("B2_KEY_ID")
   app_key = app_key or os.getenv("B2_APPLICATION_KEY") or os.getenv("B2_APP_KEY")
   bucket  = bucket  or os.getenv("B2_BUCKET_NAME")     or os.getenv("B2_BUCKET")
   endpoint_override = os.getenv("B2_ENDPOINT")  # explicit wins over region-derived
   ```
   Log at `INFO` when both variants are set, explaining precedence. Pure additive.

3. **`BaseSink.emit_bytes(data, *, media_type, suggested_name=None) -> Asset`.**
   New abstract method on `BaseSink`. Contract: sink computes the key (respects
   `KeyStrategy` — CONTENT_ADDRESSABLE by default for dedup), uploads, returns a populated
   `Asset`. Default implementation writes via `backend.put()`; `LocalFilesystemSink`
   writes to disk; `ObjectStorageSink` uploads to the configured bucket.

   **Returned `Asset` MUST have `sha256` and `media_type` populated** (clarification
   C.1 from `multimodal-chat-provider.md`). Without this, `step_cache_key`'s
   `a.sha256 or a.url` branch falls back to the rotating presigned URL and breaks
   cache stability for user-uploaded inputs across runs. CAS callers and the
   NVIDIA chat workflow both depend on this guarantee.

4. **P1-14 — `LocalFilesystemSink`.**
   ```python
   class LocalFilesystemSink(BaseSink):
       root: Path
       allowed_roots: list[Path] | None = None  # sandboxing
       key_strategy: KeyStrategy = KeyStrategy.HIERARCHICAL
       def emit_bytes(self, data, *, media_type, suggested_name=None) -> Asset: ...
       def put(self, key, data, *, content_type=None, extra_args=None): ...
   ```
   `put()` writes to `self.root / key` after validating against `allowed_roots`. Returns
   a `file://` URL.

5. **P1-19 — Range reads + streaming on `StorageBackend`.**
   ```python
   def get_range(self, key: str, *, offset: int, length: int) -> bytes: ...
   def stream(self, key: str, *, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]: ...
   ```
   Abstract-level additions. S3 implementation uses `Range:` HTTP header on `GetObject`.
   Local backend reads `seek`+`read` slices. Document presigned-URL escape-hatch in the
   same PR.

6. **P1-20 — `SyncProvider.emit_bytes(data, media_type, suggested_name=None) -> Asset`.**
   Reads `self._active_sink` (set by Pipeline at run start), delegates to
   `self._active_sink.emit_bytes(...)`. Raises if no sink is active. Keeps custom
   providers out of boto3.

**Tests:**
- `test_local_filesystem_sink.py` — round-trip bytes → manifest → verify.
- `test_sink_emit_bytes.py` — every sink implementation passes the same contract test.
- `test_storage_range_streaming.py` — `get_range` returns bytes N..N+M; `stream` yields
  complete object across chunks; works for both backends.
- `test_user_agent_base.py` — boto3 Config records the expected header.
- `test_b2_env_aliases.py` — precedence matrix (both set, each set alone, neither set).

**Expected LOC:** ~550.

### 2B — Sink consumers

Covers: **P1-15.**

1. **P1-15 — `PipelineResult.save_manifest(path, *, sidecars=True)`.**
   Walks `result.steps`, collects assets, computes/validates `sha256`, writes the
   canonical manifest to `path`, and (if `sidecars=True`) emits per-asset `.c2pa.json`
   sidecars alongside each asset. Uses the active sink if one is configured; otherwise
   writes to disk directly.

2. Also: **`PipelineResult.write_sidecars_for_assets()`** as a convenience that defers
   to the same helper with `sidecars_only=True`.

**Tests:**
- Round-trip: `pipeline.run(...).save_manifest(tmp / "run.manifest.json")` → reload →
  `verify()` passes.
- Sidecar + inline paths produce byte-identical manifests (asset hashes match).

**Expected LOC:** ~150. Ships alongside or one PR after 2A.

## Wave 3 — Pipeline semantics (breaking changes behind deprecation)

Two PRs, independent surface from Wave 2 so they can run in parallel.

### 3A — Lineage (P0-01 + P2-33)

**Decision:** restore `from_result()`'s old hydration behavior + add `with_parent(run_id)`
as the documented public API. Rationale: (a) the 0.2.1 narrowing was a silent break, not
a design decision; restoring it is cheaper than asking users to migrate; (b) `with_parent`
covers the DB-loaded case without forcing callers to hold the `PipelineResult`.

```python
class Pipeline:
    def from_result(self, result: PipelineResult) -> Self:
        """Hydrate completed steps so `input_from` can reach them. Sets parent_run_id too."""
        self._parent_run_id = result.run.run_id
        self._hydrated_steps = list(result.steps)  # restores pre-0.2.1 behavior
        return self

    def with_parent(self, run_id: str) -> Self:
        """Set lineage from a bare run_id (no steps hydrated). For DB-loaded iteration."""
        self._parent_run_id = run_id
        return self
```

Also: at `.step(input_from=...)` build time, raise `PipelineBuildError` if the index
is unreachable with a pointer to the `image=` param pattern. Turns the late-runtime
"index out of range for step 0" into an actionable early error.

**Tests:**
- `test_from_result_hydration.py` — cross-pipeline `input_from` reaches hydrated step.
- `test_with_parent.py` — lineage set, no hydration, build-time error on unreachable
  `input_from`.

**Additive** — no deprecation needed.

### 3B — `run(cache=)` alias + `modality` default deprecation

**P1-03** — accept `cache=` on `Pipeline.run()` as an alias that calls `.cache(cache)`
internally (additive, no break).

**P1-10** — `Pipeline.step(modality=...)` default removal. Two-step rollout:
- Now (next minor): emit `DeprecationWarning("`modality` will be required in 0.4.0; infer from provider.get_capabilities() or set explicitly")` when omitted. Infer from provider capabilities when exactly one modality matches; if ambiguous, still default to `IMAGE` but warn with higher severity.
- +1 minor: remove the default, make `modality` a required kwarg.

Uses the Wave 0.3 `@deprecated` helper.

**Tests:**
- `run(cache=cache)` and `.cache(cache).run()` produce identical results.
- Omitting `modality` emits `DeprecationWarning`; passing it suppresses the warning.

## Wave 4 — Analysis primitives

**Biggest conceptual lift.** Needs a design doc first, then implementation. Design doc
lives at `docs/exec-plans/active/analysis-primitives-design.md` and covers the
below decisions before code lands.

### Scope

- **P0-04** — `Pipeline.input(asset_or_path)` / `Pipeline.from_asset(path)`.
  **Note (2026-04-28):** the `Asset`-passthrough subset of P0-04 shipped early
  as `Pipeline.step(external_inputs=[Asset, ...])` to unblock multimodal
  first-step calls into `NvidiaChatProvider`. `Pipeline.input(asset_or_path)`
  remains in scope as **sugar over the primitive** that adds `str | Path`
  acceptance via `LocalFilesystemSink` (Wave 2A). Both APIs will coexist; the
  fluent form is the discoverable entry point, the kwarg is the explicit one.
- **P0-05** — `StepType.{INGEST, TRANSCRIBE, CLASSIFY, ANALYZE, EXTRACT, MODERATE}`.
- **P0-06** — `Step.output: dict | None` + `Asset.text: str | None`
  (mutually exclusive with `url`).
- Introduce `AnalysisProvider(BaseProvider)` base — returns `output: dict` instead of
  (or in addition to) asset URLs.

### Design decisions (must resolve before coding)

1. **`Asset.text` vs `TextAsset` subclass.** Recommend: **`Asset.text: str | None`**
   with a validator enforcing `(url, text)` mutual exclusivity. Reasons:
   - Preserves a single `Asset` type in `Step.assets: list[Asset]` — no union narrowing,
     no schema `oneOf`.
   - Sink implementations only need one branch (`asset.text` → write to `.txt` sidecar
     vs `asset.url` → upload/copy).
   - Canonical hash stays single-typed.

2. **`Step.output` canonicalization.** The `output` dict **must** go through
   `canonical_json()` when hashing. Add unit test that asserts byte-identical hash
   across ordering-permuted equivalents.

3. **Hash coverage.** New fields included in `manifest.canonical_hash` **only when
   non-null**. Existing manifests (no `output`, no `text`) continue to verify after
   upgrade. Add a round-trip conformance test: golden manifest fixture pre- and
   post-schema-change produces the same hash.

4. **`Pipeline.input(asset_or_path)` semantics.** Seeds a virtual step `-1` with a
   synthetic `StepType.INGEST` step containing the provided Asset. `input_from=[-1]`
   references it. Accept `str | Path | Asset`; strings and Paths are resolved via
   `LocalFilesystemSink` (from Wave 2). A string URL (`https://`) becomes an Asset
   with `url=`; a local path becomes an Asset with `url="file://..."` and auto-computed
   `sha256`.

   **Asset passthrough must be cleanly chainable** (clarification C.2 from
   `multimodal-chat-provider.md`): `Pipeline.input(sink.emit_bytes(bytes, media_type=...))`
   should work end-to-end without wrapping or copy. The Asset variant is the
   primary path for user-uploaded multimodal inputs feeding chat / analysis steps.

### Implementation steps

1. `libs/spec/schemas/manifest/v1/step.schema.json` — add new StepType enum values
   and optional `output` property.
2. `libs/spec/schemas/manifest/v1/asset.schema.json` — add optional `text` property
   with a schema-level check that exactly one of `url`/`text` is set.
3. Bump `@genblaze/spec` to 0.4.0.
4. `libs/core/genblaze_core/models/enums.py` — add enum members.
5. `libs/core/genblaze_core/models/step.py` — add `output` field.
6. `libs/core/genblaze_core/models/asset.py` — add `text` field + model validator.
7. `libs/core/genblaze_core/providers/base.py` — add `AnalysisProvider` class that
   overrides `fetch_output` to populate `step.output` instead of (or alongside)
   `step.assets`.
8. `libs/core/genblaze_core/pipeline/pipeline.py` — add `Pipeline.input()` +
   `Pipeline.from_asset()`; extend `input_from` resolution to handle virtual step `-1`.
9. Regenerate `libs/spec/ts/genblaze.d.ts` via `make ts-types`.
10. Conformance tests updated (Wave 0 suite extends to new enums and fields).

### Tests

- `test_analysis_provider.py` — `AnalysisProvider` subclass returns `output` dict;
  `Step.output` survives round-trip through canonical JSON hash.
- `test_text_asset.py` — validator rejects `Asset(url=..., text=...)` (both set) and
  `Asset()` (neither set).
- `test_pipeline_input.py` — `Pipeline.input(path).step(...input_from=[-1])` resolves
  the seeded asset; also works with URL and `Asset` inputs.
- `test_canonical_hash_backcompat.py` — pre-upgrade manifest (no `output`, no `text`)
  re-verifies against the new schema.

### Risk

- **AGENTS.md canonical-hash invariant is load-bearing.** Every field added must be
  covered by the golden-vector test suite. Python↔TS divergence in how `output` is
  serialized would silently break cross-language `verify()`.
- Schema bump = breaking for strict consumers. Use a spec minor bump (0.4.0), document
  prominently, ship the TS types in the same PR.

**Expected LOC:** ~600 (code) + ~300 (tests) + schema regen.

## Wave 5 — Provenance correctness

Covers: **P1-17.**

### Design decision required first

Three options from feedback.md:
- (a) Dual-hash: record `sha256_source` (pre-embed) and `sha256_embedded` (post-embed).
- (b) Sidecar-default: inline-embed requires explicit opt-in; verify() works on sidecars
  by default.
- (c) Field rename: make it obvious `sha256` means pre-embed.

**Recommendation:** **(b) sidecar-default + (a) dual-hash as the inline-embed opt-in.**

- Sidecar is safer, always verifiable, cross-language-clean.
- Inline-embed remains available via explicit `mode="inline"` argument; when used, the
  embedder populates `asset.sha256_embedded` alongside `asset.sha256` (pre-embed).
- `verify()` checks `sha256` for sidecar-mode, `sha256_embedded` for inline-mode
  (distinguishable by embedding-presence detection).

### Implementation

1. `libs/core/genblaze_core/models/asset.py` — add `sha256_embedded: str | None = None`
   (only populated when inline-embedded).
2. `libs/core/genblaze_core/media/embedder.py` — `SmartEmbedder.embed(..., mode="sidecar")`
   default; `mode="inline"` records post-embed hash.
3. `libs/core/genblaze_core/media/verify.py` — verify both paths.
4. Schema + TS type regen.
5. Docs: `docs/features/provenance.md` — "when to use which" decision box
   (resolves feedback P3-11).

### Tests

- `test_embed_sidecar_default.py` — default mode produces a verifiable sidecar.
- `test_embed_inline_hash.py` — inline mode populates `sha256_embedded`; verify passes.
- `test_embed_backcompat.py` — existing sidecar-only manifests (no `sha256_embedded`)
  still verify.

**Expected LOC:** ~250.

## Wave 6 — Provider additions

### 6A — `GeminiImageProvider` (P1-06)

New `libs/connectors/google/genblaze_google/gemini_image.py` (or a new
`libs/connectors/google-gemini/` package — discuss with maintainer). Ships alongside
existing `ImagenProvider`. Own `ModelRegistry` slice covering `gemini-*-flash-image`
model family. Uses `google-genai` SDK (distinct from `google-generativeai` / Imagen
path).

Passes Wave 0 conformance suite on day one.

**No dependencies.** Can run in parallel with Wave 4/5.

**Expected LOC:** ~250 (mirror `ImagenProvider` shape).

### 6B — `WhisperProvider` (P1-05)

**Depends on Wave 4.** First consumer of `AnalysisProvider`:

```python
class WhisperProvider(AnalysisProvider):
    def submit(self, step): ...  # uploads audio, gets transcription job
    def poll(self, job_id): ...
    def fetch_output(self, step, result) -> Step:
        step.output = {
            "transcript": result.text,
            "language": result.language,
            "words": [...],  # populates AudioMetadata.word_timings slot that exists today
        }
        step.assets = [Asset(text=result.text, sha256=sha256(result.text), media_type="text/plain")]
        return step
```

Ships with cost model (per-second input audio). Long-audio chunking (feedback P2-14) is
a follow-up item — basic provider works for Whisper's 25MB limit out of the box.

**Expected LOC:** ~300.

## Wave 7 — First-30-minutes polish

### 7A — `genblaze-cli` release + README quickstart fix (P1-13)

1. Cut `genblaze-cli==0.1.0` to PyPI.
2. Add to `libs/meta/pyproject.toml`:
   ```toml
   [project.optional-dependencies]
   cli = ["genblaze-cli>=0.1.0,<0.2"]
   ```
3. README: `pip install genblaze[cli]` path documented.
4. Fix README quickstart to use `genblaze_core.mock.MockVideoProvider` (now pytest-free
   after Wave 1.2), OR explicitly tell the reader to also
   `pip install genblaze[gmicloud]` before running.

### 7B — README docs cliff fix (P1-18)

1. Inline the 5–6 most-referenced recipes into `README.md` as fenced code blocks:
   - Offline quickstart (hydrate + manifest + verify, no keys)
   - Fan-out / batch_run
   - Review gate (agent loop + evaluator)
   - Custom provider (the P3-20 example)
   - Cost-by-tag (uses `Run.metadata`)
   - Large-file generation (uses the Wave 2 streaming path)
2. Ship `examples/` inside the `genblaze-core` wheel via
   `[tool.hatch.build.targets.wheel].include = ["examples/"]`.
3. Resolve all README cross-links to absolute GitHub URLs (so PyPI renders them).
4. Add a minimal `MANIFEST.in` entry if needed for sdist consistency.

**Expected LOC:** mostly docs, ~200 README additions + small packaging changes.

## PR sequencing summary

| Wave | PRs | Blocking | Target LOC/PR | Can parallelize? |
|------|-----|----------|----------------|-------------------|
| 0 | 1 | Everything | 200 | no (foundation) |
| 1 | 3–4 | Wave 0 (suite) | 100–200 | yes (across items) |
| 2 | 2 (A+B) | Wave 0 | 550 + 150 | no within wave |
| 3 | 2 (A+B) | Wave 0 deprecation helper | 200 each | yes w/ Wave 2 |
| 4 | 1 (design doc) + 1 (code) | Wave 0 | 900 total | no (big PR) |
| 5 | 1 | Wave 4 | 250 | no (post-Wave 4) |
| 6 | 2 (6A, 6B) | 6A no, 6B needs Wave 4 | 250–300 | yes (split) |
| 7 | 1 (combines 7A+7B) | All above shipped | 200 | no (last) |

**Rough calendar (full-time owner):** Waves 0+1 week 1; Waves 2+3 week 2; Wave 4 weeks
3–4 (including design doc review); Wave 5 + 6A week 5; Wave 6B + Wave 7 week 6.
**Team-able:** Waves 2 and 3 can be different owners once Wave 0 lands.

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Wave 4 canonical-hash regression silently invalidates existing manifests | Golden-vector cross-wave test; add pre-Wave-4 manifest fixture that must re-verify post-Wave-4 |
| Wave 3 `modality` deprecation triggers warning spam in user test suites | One-minor-window + `PYTHONWARNINGS=default::DeprecationWarning:genblaze_core` pattern documented |
| Wave 5 dual-hash path gets out of sync with TS `verify()` | Wave 4's golden-vector test suite extends to both modes in Wave 5 |
| Wave 1 conformance test fails on connectors we don't own tests for | Start the suite with only the `models=` kwarg check; grow assertions as each connector lands its Wave 1 fix |
| `AnalysisProvider` + `emit_bytes` ship in different PRs and a Whisper consumer writes an "orphan" asset | Don't merge Wave 6B until Wave 4 + Wave 2 both ship on main; enforce via CI gate on Wave 6B PR |
| Spec bump `@genblaze/spec` 0.4.0 breaks downstream TS consumers | Ship TS types + Python models atomically in Wave 4; document breaking wire changes prominently |
| Deprecation helper emits warnings that break `make test` with `-W error` | Helper uses `DeprecationWarning`, suppress by default in our own test runner; user-facing behavior unchanged |

## Open decisions to confirm before coding

1. **P1-17 sidecar-default semantics** — recommendation is (b)+(a); confirm with
   maintainer before Wave 5.
2. **`Asset.text` vs `TextAsset`** — recommendation is `Asset.text`; confirm before
   Wave 4.
3. **Gemini image connector placement** — extend `genblaze-google` or new
   `genblaze-google-gemini`? Recommendation: extend existing for now, split only if
   `google-genai` SDK version pins conflict with Imagen's `google-generativeai`.
4. **Canonical-hash policy for new optional fields** — "exclude when null" vs "include
   as explicit null." Recommendation: exclude when null to maintain backward-compatible
   hashes on upgrade. Flag this in the spec-bump CHANGELOG entry.

## Out of scope for this plan (explicit)

- P2 and P3 rows (tracked in feedback.md, not included here).
- New connectors beyond Gemini image + Whisper.
- Full `@genblaze/manifest` (ts canonical-hash) — deferred per `ts-type-codegen.md`.
- Embedding encryption, C2PA/JWS signing — tracked in `tech-debt-tracker.md`.
- `libs/cli/` feature expansion beyond the 0.1.0 release.

## Feedback.md row coverage matrix

| ID | Wave | Notes |
|----|------|-------|
| P0-01 | 3A | + P2-33 |
| P0-03 | 1.1 | conformance-suite assertion |
| P0-04 | 4 | virtual step -1 |
| P0-05 | 4 | enum additions |
| P0-06 | 4 | Step.output + Asset.text |
| P1-01 | 1.2 | `genblaze_core.mock` split |
| P1-02 | 1.4 | validator |
| P1-03 | 3B | alias |
| P1-04 | 1.5 | ThreadPoolExecutor |
| P1-05 | 6B | depends on Wave 4 |
| P1-06 | 6A | independent |
| P1-08 | 1.6 | moderation coverage |
| P1-09 | 1.7 | Path.resolve |
| P1-10 | 3B | deprecation window |
| P1-11 | 1.8 | capability preflight + ops |
| P1-12 | 2A | env aliases |
| P1-13 | 7A | CLI release + README fix |
| P1-14 | 2A | LocalFilesystemSink |
| P1-15 | 2B | save_manifest |
| P1-16 | 1.3 | lazy urllib3 |
| P1-17 | 5 | sidecar-default + dual hash |
| P1-18 | 7B | README inline + wheel includes |
| P1-19 | 2A | get_range/stream |
| P1-20 | 2A | SyncProvider.emit_bytes |
| P1-21 | 2A | user_agent_base |

All 25 P0+P1 rows covered.
