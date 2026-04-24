<!-- last_verified: 2026-04-24 -->
<!-- cleaned: 2026-04-24 -->
# SDK Feedback Tracker

Living inbox for feedback from real users and agent-driven sample builds. Each entry is
triaged through an architect's lens: symptom → root cause → resolution shape → blast
radius (additive vs. breaking). Items graduate out of this file once they land in an
exec-plan in `active/` or ship to `completed/`.

## How to use this doc

- **Add new feedback at the top of `## Inbox`** with date, source, and one line of symptom.
- **Triage weekly**: move items out of Inbox into the priority sections, dedupe against
  existing rows, link the evidence (`file:line`), and tag a resolution shape.
- **Graduate**: when an item gets its own exec-plan, replace the row body with a link to
  the plan in `active/` and move the detail there. When the plan ships, strike the row
  and move it to `### Resolved` (kept for release-notes cross-reference).
- **Don't duplicate** `tech-debt-tracker.md` — that file tracks *internal* debt (things we
  know are wrong). This file tracks *external* pain (what users & agents hit).

Priority key:
- **P0** — blocking regression, silent contract break, or friction that 8+ of 10 sample
  agents hit.
- **P1** — high-impact bug or missing primitive with a real workaround cost.
- **P2** — ergonomics / standards drift / additive API gap.
- **P3** — docs, naming, and minor polish.

Resolution shape key: **A** additive (no break), **B** breaking (needs deprecation window),
**D** docs-only, **F** fix-in-place (bug).

## Executive summary (2026-04-24)

Four feedback corpuses merged so far: a maintainer session covering 0.2.1 regressions,
a 10-agent sampleapps survey, an install-path / worker-eval walkthrough, and an
app-builder batch focused on dependency isolation, provenance semantics, and business
workflows. The dominant themes:

1. **Analysis-shaped workflows are second-class.** The SDK is modeled around *generation*;
   ingest/transcribe/classify/moderate pipelines force throwaway providers, fake
   `data:`/`file:///` URLs, and payload-smuggling through `metadata`. (items P0-04,
   P0-05, P0-06, P1-08, P1-09, P1-14)
2. **Silent contract narrowing in 0.2.1.** Two open cases where documented or previously-
   working surfaces started failing at runtime with misleading errors
   (`from_result`, `GMICloudBase(models=...)`). Video slug direction was addressed in
   0.2.2 (see R-06). Still needs a documented deprecation discipline before the next
   tranche of fixes. (items P0-01, P0-03)
3. **Provider coverage gaps partially closed.** `genblaze-openai`/`genblaze-google`/
   `genblaze-gmicloud` standalone `chat()` wrappers shipped in 0.2.4 (R-08); Whisper
   (STT) and Gemini image (`gemini-*-flash-image`) are still missing. (items P1-05,
   P1-06)
4. **Install-time & discoverability friction — mostly closed.** The `genblaze` umbrella
   metapackage (R-07) and PyPI metadata (R-10) shipped in 0.2.3. Still open: `genblaze-cli`
   is in the README but not on PyPI; `genblaze_core.testing` imports pytest,
   `storage/transfer.py` imports urllib3, and `sinks/parquet.py` hard-fails without pyarrow
   — all at module import time; `ModelRegistry` / `ModelSpec` / `RunnableConfig` aren't in
   top-level `__all__`. (items P1-01, P1-13, P1-16, P2-01, P3-18)
5. **Provenance correctness needs a clear story.** Inline embedding mutates bytes after
   `Asset.sha256` is recorded, producing deliverables that don't verify against the
   manifest. The full-embed + redacted case is guarded; the non-redacted case isn't.
   Since provenance integrity is the SDK's differentiator, this needs either a
   dual-hash record or a sidecar-default policy. (items P1-17, P2-30, P2-31, P3-11,
   P3-17)
6. **Business-workflow metadata is possible but not obvious.** `Run.metadata` and
   `Step.metadata` slots exist but aren't wired through the fluent builder;
   `PipelineTemplate` only renders prompts, not step params; there's no per-batch
   correlation key. Campaign / SKU / locale / reviewer data ends up in provider params
   or side indexes. (items P2-16, P2-25, P2-28)
7. **Introspection and factory ergonomics are thin.** Class-level model catalogs, `b2_sink`
   / `default_tracer` presets, a public `emit_progress()`, and a `check_models()` helper
   would collapse boilerplate across every sample. (items P2-*)

The `[Unreleased]` CHANGELOG already resolves the StreamEvent typing family (Pydantic
discriminated union + JSON Schemas under `libs/spec/schemas/events/` + TS
`genblaze.d.ts`). Those items are in `### Resolved` below — do not re-open.

## Inbox

_(Empty — all 2026-04-24 feedback triaged into the priority sections below.)_

## P0 — Blocking

| ID | Title | Shape | Evidence | Notes |
|----|-------|-------|----------|-------|
| P0-01 | `from_result()` silently narrowed to lineage-only | **B** | `libs/core/genblaze_core/pipeline/pipeline.py:177` | Was the documented path to hydrate completed steps so `input_from` could reach across runs; now only sets `_parent_run_id`. Receiving step fails with `input_from index 0 is out of range for step 0 (only 0 prior steps completed)` — error blames the wrong site. **Resolution:** either (a) restore hydration, (b) add sibling `hydrate_from(result)`, or (c) raise at `.step(input_from=...)` build-time when the index is unreachable, pointing at the `image=` param pattern. Needs a deprecation window. |
| P0-03 | `GMICloudBase.__init__` drops the documented `models=` kwarg | **F** | `libs/connectors/gmicloud/genblaze_gmicloud/_base.py:90` | `BaseProvider(*, models=None)` exposes it and the docstring tells users to override there; `GMICloudBase` calls `super().__init__()` bare, so `Provider(models=reg)` raises `TypeError`. **Resolution:** one-line fix (add `models: ModelRegistry \| None = None` to the signature, forward to super) **plus** a cross-provider conformance test asserting every `BaseProvider` subclass accepts `models=` without error. |
| P0-04 | No `Pipeline.input(asset_or_path)` — first step must be a generator | **A** | `libs/core/genblaze_core/pipeline/pipeline.py` | Top friction point in the 10-agent survey (hit by 8/10). Forces throwaway `SyncProvider` subclasses (`LocalFileProvider`, `PassthroughProvider`, `MockVideoProvider`) just to seed step 0 with an existing file or URL. **Resolution:** add `Pipeline.input(asset_or_path)` / `Pipeline.from_asset(path)` that hydrates a virtual step -1 so `input_from=[-1]` or implicit first-arg resolution works. Pairs well with P0-05 (analysis StepTypes). |
| P0-05 | SDK is generation-shaped; analysis workflows don't fit | **A** | `libs/core/genblaze_core/models/enums.py:37` | `StepType` = `{GENERATE, UPSCALE, TRANSCODE, MIX, EDIT, CUSTOM}`. No `INGEST, TRANSCRIBE, CLASSIFY, ANALYZE, EXTRACT, MODERATE`. Hit by 7/10 agents. Analysis results get smuggled through `metadata` or written to fake `file:///data:` URLs. **Resolution:** extend `StepType` + introduce `AnalysisProvider` base that returns structured output instead of asset URLs (see P0-06). |
| P0-06 | `Step.output` / `Asset.text` missing — text & JSON are second-class | **A** | `libs/core/genblaze_core/models/step.py:22`, `libs/core/genblaze_core/models/asset.py:76` | `Step` has `assets` (URL outputs) and `metadata` only — no `output: Any` field for structured JSON. `Asset.url: str` is mandatory with no `text` field, so transcripts/summaries/JSON reports get stuffed into `metadata["text"]` or `data:text/plain;base64,...` URLs (sink behavior on data-URIs is undefined). Hit by 4/10 agents. **Resolution:** add `Step.output: dict \| None` **and** either `TextAsset` or `Asset.text: str \| None` (mutually exclusive with `url`). Design needed to pick one — avoid both. |

## P1 — High impact

| ID | Title | Shape | Evidence | Notes |
|----|-------|-------|----------|-------|
| P1-01 | `genblaze_core/testing.py` top-level `import pytest` | **F** | `libs/core/genblaze_core/testing.py:41` (also houses `MockVideoProvider`:123 / `MockAudioProvider`:152) | `from genblaze_core.testing import MockProvider` / `MockVideoProvider` / `MockAudioProvider` all fail with `ModuleNotFoundError: pytest` outside test envs. Worker scripts and offline eval runs can't use the built-in mocks — every sample reinvents a fake provider. Confirmed across multiple feedback batches. **Resolution (preferred):** split the three mock classes into a pytest-free module (`genblaze_core.mock`); keep pytest-specific fixtures under `genblaze_core.testing`. Alternative: publish `genblaze-core[testing]` extra and document on the first install page. |
| P1-02 | `PromptTemplate("literal")` crashes; only kwarg form works | **F** | `libs/core/genblaze_core/models/prompt_template.py:11` | Positional form is shown in README and `examples/batch_with_templates.py` — Pydantic rejects it. Shipped example is broken. **Resolution:** add a `__init__(self, template=None, /, **data)` shim (or `model_validator(mode='before')`) that accepts one positional string. |
| P1-03 | `Pipeline.run(cache=...)` raises TypeError | **D** or **A** | `libs/core/genblaze_core/pipeline/pipeline.py:836` | `cache` is fluent (`.cache(...)`), not a `run` kwarg. Discoverable-API failure. **Resolution:** docs callout in quickstart **or** accept `cache=` as an alias in `run()`. |
| P1-04 | `batch_run` sync path is serial; `max_concurrency` only applies in `abatch_run` | **F** | `libs/core/genblaze_core/pipeline/pipeline.py:1362` | 500-track footgun — the advertised knob is silently ignored. **Resolution:** use a ThreadPoolExecutor bounded by `max_concurrency` in the sync path, or raise at build time if `max_concurrency>1` and caller used sync. |
| P1-05 | No Whisper (STT) provider in `genblaze-openai` | **A** | `libs/connectors/openai/genblaze_openai/__init__.py` (exports `SoraProvider`, `DalleProvider`, `OpenAITTSProvider`, `chat` wrappers — no Whisper) | Chat / text-completion coverage shipped in 0.2.4 (see R-08). **STT remains open:** every transcription sample still hand-rolls a `BaseProvider`. `AudioMetadata.word_timings: list[WordTiming]` slot exists but nothing populates it. **Resolution:** ship `WhisperProvider` (sync + async). Ties to P0-05 analysis StepTypes — a transcription step's output is structured JSON, not a media URL. |
| P1-06 | No Gemini image provider in `genblaze-google` | **A** | `libs/connectors/google/genblaze_google/__init__.py` | Exports only `VeoProvider`, `ImagenProvider`. Nano Banana / `gemini-*-flash-image` are delivered via `google-genai`, not Imagen API — folding into `ImagenProvider` would be wrong. Caused a Risk-B STOP on a sample build. **Resolution:** new `GeminiImageProvider` (own model registry slice). |
| P1-08 | `ModerationHook.check_prompt` silently skipped when `step.prompt is None` | **F** | `libs/core/genblaze_core/pipeline/pipeline.py:402` | UGC pipelines that feed text through `input_from` or metadata bypass moderation entirely. Security-affecting. **Resolution:** also run moderation against resolved `input_from` text payloads (ties to P0-06 once `Asset.text`/`Step.output` exists). |
| P1-09 | `DalleProvider` allowed-file-roots rejects `file:///tmp/...` on macOS | **F** | `libs/connectors/openai/genblaze_openai/dalle.py` (allowed-roots resolver) | `/tmp` resolves to `/private/var/folders/...` via Darwin symlinks; allowlist doesn't canonicalize. **Resolution:** `Path.resolve()` on both sides of the allowlist check, or normalize via `os.path.realpath`. Ties to P1-10 (P0-11-06 from prior plan, sandboxed `file://` reads). |
| P1-10 | `Pipeline.step()` default `modality=Modality.IMAGE` | **B** | `libs/core/genblaze_core/pipeline/pipeline.py` (`.step()`) | Surprising default for an AV-centric SDK. **Resolution:** make `modality` required, or default based on provider's `get_capabilities()`. Breaking — needs deprecation warning when omitted. |
| P1-11 | `FFmpegTransform` missing core ops + `overlay_text` has no capability preflight | **A** + **F** | `libs/core/genblaze_core/providers/ffmpeg.py` (ops), transform impl | Missing: `trim`, `extract_audio`, `concat`, `split`, `atempo`, `replace_audio`, **audio mixdown (pre-mux)**, **multi-track audio** (layered music/VO/SFX). `overlay_text` silently requires `libfreetype` (macOS homebrew default omits it) — raw ffmpeg exit code 8 surfaces instead of a capability check. **Resolution:** preflight `ffmpeg -filters` once at init for text ops; add the missing ops (each ~15 LOC). Pairs with P2-23 (image compositor) and P3-19 (`FFmpegCompositor` file-roots + multi-input docs). |
| P1-12 | B2 env-var names conflict with parent `sampleapps/` standard | **D** or **B** | `libs/connectors/s3/genblaze_s3/backend.py:339` | Genblaze: `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET`. Sampleapps standard: `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`. Every sample either breaks the standard or needs an aliasing shim. **Resolution:** accept both pairs in `for_backblaze(...)` with a precedence doc note, **or** pick one and deprecate the other. Document prominently either way. |
| P1-13 | `genblaze-cli` advertised in README but not on PyPI | **D** or **A** | `README.md:93` (`pip install genblaze-cli`), `README.md:365-367` (`genblaze extract/verify/replay`); source lives at `cli/pyproject.toml` (name = `genblaze-cli`, version `0.1.0`, entry point `genblaze = "genblaze_cli.main:cli"`) | `pip install genblaze-cli` fails; isolated venvs have no `genblaze` executable. Package exists in the repo but has no PyPI release. **Resolution:** either (a) cut a `genblaze-cli==0.1.0` release and add it to `scripts/release.sh` / the `/release-check` skill's known-package list, or (b) temporarily replace the README section with an "install from source" note (`pip install -e cli/`) until the first release. Do one or the other — the current state sets a broken first impression. |
| P1-14 | No `LocalFilesystemSink` / `LocalArchiveSink` for offline evaluation | **A** | `libs/core/genblaze_core/storage/sink.py` (only `ObjectStorageSink` is exported); `examples/quickstart_local.py` works around it with manual manifest writes | Real evaluation / CI workflows frequently generate local `file://` assets before any upload. `ObjectStorageSink` is object-storage-only and exposes no `allowed_roots` option for workspace-local files, so workers hand-roll sinks that write assets + sidecars + manifest to disk. **Resolution:** add `LocalFilesystemSink(root, *, allowed_roots, key_strategy)` sibling of `ObjectStorageSink` implementing the same `BaseSink` contract, and document the offline flow (generate → hash → manifest → sidecars → verify) in a `docs/features/local-workflows.md` page. Pairs with P0-04 (`Pipeline.input`) and P1-15 (manifest/sidecar helpers). |
| P1-15 | No `PipelineResult.save_manifest(path)` / `write_sidecars_for_assets()` helpers | **A** | `libs/core/genblaze_core/pipeline/result.py:37` — only has `failed_steps`, `succeeded_steps`, `error_summary`, `save` | Every offline/eval sample reinvents the same ~40 LOC: walk `result.steps`, collect assets, compute SHA-256, write a canonical manifest JSON, emit per-asset `.c2pa.json` sidecars. Sidecar-only is the safer default for hash-sensitive flows (see P1-17, P3-11). **Resolution:** `PipelineResult.save_manifest(path, *, sidecars=True)` that writes the canonical manifest and optional per-asset sidecars in one call. Pairs with P1-14. |
| P1-16 | `urllib3` leaks into `genblaze_core` import path | **F** | `libs/core/genblaze_core/storage/transfer.py:14` (`import urllib3` at module level); re-exported through `storage/sink.py:15` and `storage/__init__.py:10` | `from genblaze_core import ObjectStorageSink` fails in minimal installs that don't ship `urllib3`. Blocks local/offline evaluation flows that never touch an object-storage backend. Parallel to P1-01 (pytest) and P3-18 (pyarrow) — same anti-pattern. **Resolution:** lazy-import `urllib3` inside the transfer function that actually needs it, or pin `urllib3` as a hard dep of `genblaze-core` if transfer is always required. Covered by the "Optional dependency isolation" cross-cutting initiative. |
| P1-17 | Inline manifest embedding can invalidate `Asset.sha256` | **A** / **B** | `libs/core/genblaze_core/media/embedder.py:36`, `libs/core/genblaze_core/models/manifest.py:161-204` (full-embed with redaction already raises `ManifestError`) | Provenance-correctness gap: inline embedding mutates the media bytes **after** `Asset.sha256` is recorded, so the delivered embedded file won't verify against the manifest hash. The full-embed + redaction case is already guarded (raises), but the non-redacted full-embed case silently produces an un-verifiable artifact. The whole SDK value-prop is provenance integrity — this needs a clear story. **Resolution (pick one):** (a) compute and record both `sha256_source` and `sha256_embedded` on the `Asset` when inline-embedding; (b) make sidecar the default for any flow that wants post-delivery verification and raise on inline-embed unless the caller explicitly opts out; (c) rename the field so users understand it's pre-embed. Document the chosen model in `docs/features/provenance.md`. Decision needed before any new embedder method (P2-31) ships. |

## P2 — Ergonomics & missing primitives

| ID | Title | Shape | Notes |
|----|-------|-------|-------|
| P2-01 | Expand top-level `genblaze_core.__all__` | **A** | Already exported: `Pipeline`, `BaseProvider`, `SyncProvider`, `BaseSink`, `Asset`, `Manifest`. **Missing:** `ModelRegistry`, `ModelSpec`, `RunnableConfig`. Users discover these today only by reading installed-package source. Add them to `_LAZY_IMPORTS` in `libs/core/genblaze_core/__init__.py` and surface the full list on the first docs page. |
| P2-02 | `Pipeline.name` readable property | **A** | `self._name` exists (`pipeline.py:139`), just needs `@property`. Useful for logs, tracers, assertions. |
| P2-03 | Class-level `Provider.known_models()` + `ModelRegistry` iter/contains | **A** | `Provider.models` is an instance property — forces `Provider(api_key="dummy").models.known()` just to introspect. Add module-level `SUPPORTED_MODELS` constants too. |
| P2-04 | `S3StorageBackend.list(prefix=..., max_keys=..., continuation_token=...)` | **A** | No public list primitive — "manifest-is-the-DB" samples reach into `_client.list_objects_v2`. Pairs with P2-05 (`FileEntry`). |
| P2-05 | Export `FileEntry` / `ManifestEntry` Pydantic model | **A** | First-class `list()` return type with `key`, `size`, `last_modified`, `content_type`. |
| P2-06 | `Pipeline.fan_out(key, values, build_fn)` for per-variant parallel runs | **A** | Per-language dubs, per-prompt A/B, per-stage analysis all force hand-rolled asyncio or N disjoint pipelines with no shared parent run. |
| P2-07 | `Pipeline.astream_windowed(source, window=...)` | **A** | Live ingestion currently forces N tiny one-shot pipelines. |
| P2-08 | `bulk_ingest(paths, sink, concurrency=N)` helper with progress + resume | **A** | Every analysis sample reinvents the same ThreadPoolExecutor loop around `backend.put()`. |
| P2-09 | `genblaze_core.presets.b2_sink(backend, *, prefix="runs")` + `default_tracer(...)` | **A** | Every sample re-implements the same ~10 LOC (`ObjectStorageSink` + `KeyStrategy.HIERARCHICAL` + `LoggingTracer` + optional `OTelTracer`). |
| P2-10 | `genblaze_core.web.sse.stream_to_sse(pipeline, ...)` | **A** | First-party SSE adapter so samples don't hand-roll `json.dumps(event.to_dict())` per turn. |
| P2-11 | `genblaze_core.check_models(*pairs)` helper | **A** | Every first-run sanity check today requires spelunking `provider.models.has(...)`. Return structured `(accepted, unknown, class_missing)` report. |
| P2-12 | `provider.param_schema(model)` introspection | **A** | "Does `aspect_ratio` work for `gpt-image-2`?" requires a network call today. JSON-schema-ish return would drive form UIs and reject silently-ignored kwargs. |
| P2-13 | `Modality.EMBEDDING` + vector primitives | **A** | "Embed" in Genblaze means C2PA manifest embedding — collides with vector-embedding terminology in every RAG/search sample. Namespace risk; address in an exec-plan, not a one-line add. |
| P2-14 | Long-audio chunking helper | **A** | Whisper's 25MB ceiling hits real podcasts; every transcription app reinvents silence-split → N parallel → timestamp-stitch. |
| P2-15 | `Asset.key` / `Asset.backend_key` accessor | **A** | Custom providers parse keys out of URL strings today. |
| P2-16 | Pipeline-level idempotency + per-batch correlation hooks | **A** | `StepCache` covers step work, not published-output dedup. Batch flows (N variants for a single campaign/SKU/locale) also need a correlation key that survives across the N runs so downstream systems can group deliveries. **Resolution:** `Pipeline.idempotency_key(...)` + `batch_run(correlation_key=...)` that plumbs through to `Run.metadata` and the webhook payload. |
| P2-17 | Webhook SSRF dev-mode allowlist **+** local capture/test transport | **A** | `libs/core/genblaze_core/webhooks/notifier.py:70-82` explicitly rejects `localhost` and non-HTTPS. `example.test` hostnames are unusable in local testing today, and there's no in-process capture transport to assert delivery shape in unit tests. **Resolution:** `WebhookConfig(dev_mode=True)` that permits `localhost` / `example.test` / non-HTTPS with a loud log warning; ship a `CapturingWebhookTransport` that records calls in-memory for tests. |
| P2-19 | `S3StorageBackend.ping()` + typed exception hierarchy | **A** | Samples write `backend.exists("__health_probe__")` inside `try/except Exception`. |
| P2-21 | `Pipeline.stream()` handle carries `run_id` and terminal `result` | **A** | Samples hand-roll `_run_store: dict[str, Any]` to correlate the final `PipelineResult` with the originating `run_id`. **Resolution:** `stream = pipeline.stream(...); stream.result` after iteration; `stream.run_id` once streaming starts. |
| P2-22 | Image providers populate `StreamEvent.preview_url` | **A** | Field exists in `events.py` but image providers don't populate it — frontend pokes `step?.assets?.[0]?.key` to render previews as events arrive. |
| P2-23 | `FFmpegCompositor` is video-only | **A** | No image compositor for side-by-side comparisons; hand-rolled per sample. |
| P2-24 | Vision-analysis / classifier providers | **A** | NSFW, OCR, damage detection, auto-tag all hand-rolled. Pairs with P0-05 analysis StepTypes. |
| P2-25 | `Pipeline.step(metadata=..., prompt_visibility=...)` kwargs | **A** | `libs/core/genblaze_core/pipeline/pipeline.py:186` — current signature only takes `provider, model, prompt, modality, step_type, fallback_models, input_from, **params`. `Step.metadata` already exists on the model (`step.py:59`) and `Run.metadata` exists on `Run` (`run.py:35`) — the model slots are there, just not wired through the fluent builder. Business flows (campaign/SKU/locale/reviewer/job) stuff this data into provider `params` or external indexes today. **Resolution:** surface `metadata=` and `prompt_visibility=` explicitly on `.step()`; also accept `metadata=` on `Pipeline(...)` and `.run(...)` so `Run.metadata` is reachable without the `RunnableConfig` detour (see P2-27). |
| P2-26 | Named step handles (`name="extract"`, `input_from=["extract"]`) | **A** | `_PipelineStep` has no `name` field; `input_from` is `list[int]` only. Numeric indices are fragile across refactors — reorder a step and every downstream `input_from=[2]` silently points at the wrong producer. **Resolution:** allow `name: str` on `.step()` and accept `list[int \| str]` for `input_from`, resolving strings at build time (raise on unknown name or ambiguous reuse). |
| P2-27 | `Pipeline.run(on_submit=...)` silently unsupported | **A** or **D** | `libs/core/genblaze_core/pipeline/pipeline.py:836` — `run()` does NOT accept `on_submit`; it lives on `RunnableConfig` (`runnable/config.py:13-25`) and must be passed via `.config({"on_submit": ...})`. `run()` DOES accept `timeout`, `max_retries`, `progress`, `on_progress`, `on_step_complete` (the "missing" kwargs in prior feedback are actually present — docs don't list them). **Resolution:** accept `on_submit=` on `.run()` as a convenience alias, OR keep the split and fix docs (see P3-15). Also audit that `RunnableConfig` merging preserves callbacks across `.config()` calls (unverified claim — investigate). |
| P2-28 | `PipelineTemplate` renders prompts only, not step params | **A** | `libs/core/genblaze_core/pipeline/template.py:150-174` — step params at `:173` are passed through as `**st.params` without template substitution; only `PromptTemplate.render(**variables)` runs on prompts. Per-locale / per-variant batch flows need `{locale}` substitution in `params` too (e.g., `voice=VOICE_BY_LOCALE[{locale}]`). **Resolution:** walk `st.params` for strings matching `{var}` patterns and `.format_map(variables)` them; skip anything that isn't a string template. |
| P2-29 | Fixture-backed image/audio/video providers (real local media, not URL placeholders) | **A** | Current mocks (`testing.py:41` / `:123` / `:152`) return synthetic asset URLs, not real files — so embedding examples, compositor tests, and `verify()` samples all need hand-rolled stubs that emit real PNG/WAV/MP4 bytes. **Resolution:** `LocalFixtureImageProvider` / `LocalFixtureAudioProvider` / `LocalFixtureVideoProvider` in `genblaze_core.mock` (after P1-01 split) that write tiny real-format files into a caller-supplied scratch dir and return proper `file://` asset URLs. Unblocks CI-sized examples for embedding / composition / verification flows. |
| P2-30 | Batch-embed helper on `PipelineResult` | **A** | No `result.embed_all(*, mode="sidecar")` today — apps walk steps+assets manually to invoke `SmartEmbedder`. **Resolution:** `PipelineResult.embed_all(mode="sidecar" \| "inline", output_dir=None)` — pairs with P1-15 (`save_manifest`) and blocks on P1-17 (asset-hash semantics). |
| P2-31 | `SmartEmbedder.extract()` / `.verify()` methods | **A** | `libs/core/genblaze_core/media/embedder.py:36-142` — class only has `.embed()`. Users hit a natural ask for the inverse operations and there's no discoverable path. **Resolution:** `SmartEmbedder.extract(path) -> Manifest \| None` and `SmartEmbedder.verify(path, *, expected: Manifest \| None = None) -> VerifyResult`. Semantics need to line up with P1-17's hash-field decision. |
| P2-32 | Public `emit_progress(...)` for custom providers | **A** | `libs/core/genblaze_core/providers/base.py:297-321` — `_fire_progress()` exists but is leading-underscore private; custom providers reach for it anyway. **Resolution:** rename/alias to `emit_progress(...)` (keep `_fire_progress` as a deprecated shim for one minor) and document in the provider-authoring guide (P3-10). |

## P3 — Docs & polish

| ID | Title | Notes |
|----|-------|-------|
| P3-02 | Pipeline concurrency is Pipeline-level, not run-level | Quickstart callout. Currently learnable only by reading a sibling sample. |
| P3-03 | `StreamEvent.to_dict()` vs `model_dump_json()` | **Resolution (0.2.2):** `docs/features/streaming.md` now documents `to_dict()` (= `model_dump(mode="json", exclude_none=True)`) plus the `StreamEventAdapter.validate_python(...)` inbound-parse path. |
| P3-04 | `genblaze-openai` / `genblaze-google` PyPI pages | Must list registered models verbatim and state scope (e.g., "no Whisper in this release", "Imagen only, not Gemini image") so install-time scope mismatches are caught pre-code. |
| P3-05 | Deprecation discipline callout in release notes | `0.2.1` introduced `deprecated_aliases` with `DeprecationWarning` — good — but P0-01/02/03 above will also be breaking when fixed. Commit to one-minor-version minimum deprecation windows with per-release CHANGELOG callouts. |
| P3-06 | Promote no-key offline quickstart on the first README page | `examples/quickstart_local.py`, `agent_loop_local.py`, and `streaming_local.py` already run without API keys, but the README doesn't lead with any of them. Move one to "Getting started" and link the other two — it's the fastest way to de-risk the first-install experience, and it sets up the provenance story (generate → hash → manifest → verify) before any provider creds show up. |
| P3-07 | Provider matrix on the first docs page | Single table with columns: PyPI package, import module, provider classes, supported modalities, env vars, example model IDs, whether credentials are required at import / init / submit. Addresses P3-04 (scope clarity) and the worker feedback about credential-timing ambiguity in one artifact. |
| P3-08 | Provenance cookbook | End-to-end recipe: generate assets → compute asset hashes → write canonical manifest → write sidecars → verify manifest hash → verify each file's SHA-256 → upload / assign durable storage URIs. Pairs with P1-15 (the `save_manifest` helper) so the code snippet is one call, not forty lines. |
| P3-09 | Local development guide | `docs/features/local-workflows.md`: writing a custom provider (`BaseProvider` vs `SyncProvider` selection criteria), `LocalFilesystemSink` (P1-14), sidecar-only mode, CI/test setup without provider API keys. |
| P3-10 | Document `prepare_payload(step)` in the provider-authoring guide | `prepare_payload` already exists at `libs/core/genblaze_core/providers/base.py:260` and is referenced from `docs/features/model-registry.md`, but the **authoring** guide doesn't call it out — custom providers that skip it silently lose model-registry resolution, param aliasing, input routing, and validation. Add an explicit "what to call inside `submit()`" section. |
| P3-11 | Sidecar-vs-inline embedding callout in provenance docs | Inline embedding can mutate media bytes **after** the manifest records asset hashes, which invalidates subsequent hash verification. Sidecars are safer for hash-sensitive workflows. Note: the `full + redacted` combination already raises `ManifestError` (`libs/core/genblaze_core/models/manifest.py:192-202`) — docs should also document that guard so users know why. Current docs treat inline/sidecar as equivalent. One paragraph + a "when to use which" decision box. Unblocks after P1-17 resolution. |
| P3-12 | Credential timing: import vs. init vs. submit | Workers couldn't tell from the README whether `DalleProvider(api_key=...)` validates credentials at construction time (it doesn't) or only when submitting (it does). Drives real test-setup confusion. Document per-provider in the matrix (P3-07). |
| P3-13 | `file://` asset support & URL semantics | Document the three-way distinction: local `asset.url` (`file:///...`), durable storage URI (`s3://...`, `b2://...`, `https://...`), manifest pointer URI. Which ones survive serialization? Which ones do sinks persist? Which ones does `verify()` accept? |
| P3-15 | Docs/runtime API alignment sweep | Multiple examples disagree with runtime: `PromptTemplate("literal")` (fixed in P1-02), `Pipeline.run(on_submit=...)` vs `.config({"on_submit": ...})` (see P2-27), and pipeline docs omit `timeout`, `max_retries`, `progress`, `on_progress`, `on_step_complete` even though all five kwargs exist on `.run()`. **Resolution:** one-shot audit of every example under `examples/` and every code block in `docs/features/*.md` against the live signatures; add a `make docs-check` that parses the examples. |
| P3-16 | Document `param_allowlist` silent-drop behavior + `strict_params=True` opt-in | `libs/core/genblaze_core/providers/model_registry.py:40-41, 49, 256-264` — extras are silently dropped unless `ModelRegistry(strict_params=True)`. Surprising for users who think they're passing a supported param. **Resolution:** docs callout + consider warning (not error) as the default when a param is dropped, with `strict_params=True` escalating to error. |
| P3-17 | Document `manifest_uri` exclusion from canonical hash + pointer-mode flow | `libs/core/genblaze_core/models/manifest.py:106-110` explicitly excludes `manifest_uri` from the canonical hash (transport metadata — correct design). Also: pointer-mode sidecars require a `catch → fetch → verify` flow that's not illustrated anywhere. **Resolution:** one section in `docs/features/provenance.md` covering both: the exclusion rationale + the pointer-sidecar end-to-end example. |
| P3-18 | `ParquetSink` install-extra + JSONL/SQLite analytics fallback | `libs/core/genblaze_core/sinks/parquet.py:13-20` — hard-fails at import without `pyarrow`. Teams evaluating provenance search need something that works out of the box. **Resolution:** (a) document `genblaze-core[parquet]` as the install extra (if not already); (b) ship a `JsonlSink` or `SqliteSink` with the same interface as `ParquetSink` so the first evaluation run doesn't need a heavy dep. Covered by the "Optional dependency isolation" cross-cutting initiative. |
| P3-19 | `FFmpegCompositor` file-root / multi-input semantics | `FFmpegCompositor` currently doesn't document its allowed-file-roots behavior or its handling of N≥3 inputs / mixed asset types. Users had to read the source. **Resolution:** docstring + short docs page; pairs with P1-11 (missing ops) and P2-23 (image compositor). |

## Cross-cutting initiatives

These are meta-items implied by multiple rows above. Each deserves its own exec-plan.

- **Catalog sync CI gate** — resolves P0-02 and prevents recurrence. Ship `provider.fetch_catalog()` and a CI contract test that diffs registry canonicals vs. live `/models` at publish time. (drives P0-02, P2-11 stabilization)
- **Analysis pipeline primitives** — the `AnalysisProvider` + `StepType.{INGEST,TRANSCRIBE,CLASSIFY,ANALYZE,MODERATE}` + `Step.output: dict` + `Asset.text` bundle. Single breaking design touching models, Pipeline, and at least one provider (Whisper). (drives P0-04, P0-05, P0-06, P1-05 partially, P2-13 partially, P2-24)
- **Provider-contract conformance suite** — cross-provider tests that every `BaseProvider` subclass accepts `models=`, honors `get_capabilities()`, and retries on 5xx. (drives P0-03, P1-07)
- **Breaking-change deprecation discipline** — documented policy before P0-01, P0-02, P1-10 ship. (drives P3-05)
- **First-30-minutes experience** — CLI availability + offline quickstart + provider matrix. Install-path (metapackage, PyPI metadata) resolved in 0.2.3. Goal: a new user can `pip install …` and run something end-to-end without credentials in under 5 minutes. (drives P1-01, P1-13, P1-14, P1-15, P2-01, P3-06, P3-07, P3-09)
- **Offline / local-workflows primitives** — `LocalFilesystemSink` + `PipelineResult.save_manifest()` + pytest-free mock provider + fixture-backed media providers + local-workflows doc. Evaluation and CI are a first-class use case, not a testing afterthought. (drives P1-01, P1-14, P1-15, P2-29, P3-08, P3-09, P3-11, P3-13)
- **Optional dependency isolation** — pytest, urllib3, pyarrow all currently break top-level or near-top-level imports in minimal installs. Establish a lazy-import convention and a CI job that installs `genblaze-core` with zero extras and smoke-tests `from genblaze_core import …` for every public symbol. (drives P1-01, P1-16, P3-18)
- **Provenance correctness story** — decide inline-embed vs sidecar semantics, document it, and align `SmartEmbedder` surface + `PipelineResult.embed_all()` + `param_allowlist` strict-mode around it. The SDK's differentiator is provenance integrity; the current semantics for inline embedding undermine it. (drives P1-17, P2-30, P2-31, P3-11, P3-17)
- **Business provenance modeling** — surface `Run.metadata` / `Step.metadata` through the fluent builder, let `PipelineTemplate` substitute step params, and wire batch correlation/idempotency. Workflows that track campaign/SKU/locale/reviewer/job identity currently shove this data into provider `params` or side indexes. (drives P2-16, P2-25, P2-28)

## Mapping to existing exec-plans

| Feedback ID | Already covered by |
|-------------|-------------------|
| — | `active/framework-dx-recommendations.md` — general DX tracker; this doc is the inbox feeding it |
| P1-05, P1-06 | `active/openai-image-model-expansion.md` — adjacent; extend to cover Whisper/chat & Gemini image |
| — | `active/agent-streaming-observability.md` — streaming work; most items here resolved in `[Unreleased]` |
| P2-20, P2-21, P2-22, P3-03 | `active/ts-type-codegen.md` — event typing; `libs/spec/schemas/events/` already added |
| Wave 1–3 items | `active/p0-p1-production-quality.md` — existing waves; some overlap with P1-09/P1-10/P1-11 |

## Resolved (for release-notes cross-reference)

Do not re-open. Links point to the shipped or in-flight fix.

| ID | Title | Resolution |
|----|-------|------------|
| R-01 | `StreamEvent` is Pydantic discriminated union | `libs/core/genblaze_core/observability/events.py` + CHANGELOG [0.2.3]. Ten per-variant classes under a common base. |
| R-02 | JSON Schemas for all 10 stream-event variants | `libs/spec/schemas/events/v1/` shipped in 0.2.3. |
| R-03 | TypeScript `StreamEvent` discriminated union | `libs/spec/ts/genblaze.d.ts` updated in 0.2.3; `@genblaze/spec` 0.3.0 (0.2.3) + 0.3.1 (0.2.4) on npm. |
| R-04 | `step.completed` event carries `run_id` | `libs/core/genblaze_core/pipeline/streaming.py:49` accepts and propagates `run_id`; callers pass `self.run_id` at `:120-123`. |
| R-05 | Response-envelope `unwrap_error_body()` + regression tests | Helper shipped in 0.2.1. Regression coverage landed in 0.2.2 at `libs/connectors/gmicloud/tests/test_envelope_helpers.py` — 21 direct unit tests covering current shape, legacy `*_url` fallback, `thumbnail_image_url` image fallback, malformed entries, and error-body unwrap edge cases. |
| R-06 | Video slug canonical direction fixed (was P0-02) | CHANGELOG [0.2.2] — "all image and video model ids rewritten to the live lowercase slugs the request-queue API actually accepts (`seedream-5.0-lite`, `veo3`, `wan2.6-i2v`, `kling-text2video-v1.6-pro`, etc.). Old PascalCase ids still resolve via `deprecated_aliases` and will be removed in 0.4." Submits now send the canonical slug on the wire. |
| R-07 | `pip install genblaze` installs real deps (was P0-07) | `libs/meta/pyproject.toml` — `genblaze` 0.2.3 umbrella with `genblaze-core>=0.2.2,<0.3` + `genblaze-s3>=0.2.3,<0.3` as runtime deps, curated bundles `[video]`, `[image]`, `[audio]`, `[all]`. Shipped in 0.2.3. Root-level `pyproject.toml` is the monorepo config and is not the published package. |
| R-08 | OpenAI / Google / GMICloud chat wrappers (was part of P1-05) | `libs/connectors/openai/genblaze_openai/chat.py`, `libs/connectors/google/genblaze_google/chat.py`, `libs/connectors/gmicloud/genblaze_gmicloud/chat.py`. Uniform `chat()` / `achat()` signature returning `ChatResponse`. Shipped in 0.2.4. `docs/features/llm-calls.md` documents the surface. **Whisper (STT) remains open — see P1-05.** |
| R-09 | 5xx transient retry on providers (was P1-07) | `libs/core/genblaze_core/providers/base.py:123` — transient-retry logic on `500`/`502`/`503`. Commit `93d3ea7` ("enhance error handling and logging … add transient retry logic for polling"). Dedicated `UPSTREAM_TRANSIENT` error code not yet introduced; partial. |
| R-10 | `genblaze-core` PyPI metadata — `authors`, `Homepage` (was P3-14) | `libs/core/pyproject.toml` now has `authors` + `Homepage`. CHANGELOG [0.2.3]: "Every published Python package now has `authors` and `Homepage` URL populated." |
| R-11 | `StreamEventType` namespace / variant docs (was P2-20) | Discriminated union shipped with per-variant exports from `genblaze_core.observability`; `docs/features/streaming.md` table enumerates every variant; JSON Schemas in `libs/spec/schemas/events/v1/` + TS types pin the wire contract. Shipped in 0.2.3. |
| R-12 | `ModelRegistry` API reference (was P3-01) | `docs/features/model-registry.md` methods table documents `resolve_canonical`, `has`, `get()` deprecation-warning behavior, plus "Renaming a model slug safely" section. Shipped in 0.2.2. |
| R-13 | `ProviderErrorCode.CONTENT_POLICY` | Dedicated error code for safety / content-policy refusals. `classify_api_error()` detects common keywords; per-provider mappers (Google, GMICloud) prioritize policy detection. Wire schema + TS types regenerated. Shipped in 0.2.4. |
| R-14 | Provider × modality capability matrix in root README | Includes "Chat (LLM)" column. Shipped in 0.2.4. Addresses the single-page "which connector does what?" question that was recurring in feedback. |
| R-15 | `ModelRegistry` dropped-params visibility | Bumped from `DEBUG` to `INFO` so silent allowlist drops surface in typical production logs. Shipped in 0.2.4 (note: P3-16 still proposes warning + `strict_params=True` flip — separate ergonomic question). |
| R-16 | Standalone `chat()` / `achat()` error classification | Uses same `ProviderError` / `ProviderErrorCode` taxonomy as pipeline steps. Covered in per-provider `test_chat.py` (openai, google, gmicloud). Shipped in 0.2.4. |

## Source log

- **2026-04-24 — maintainer session** — regressions hit while building a cross-pipeline
  lineage sample + GMICloud video catalog audit. Covered P0-01, P0-02, P0-03, P1-07,
  P2-18, P3-05.
- **2026-04-24 — 10-agent sampleapps survey** — ten independent agents each built a
  different sample (transcription, compare-stream, moderation, B2 gallery, live ingest,
  per-language dubs, NSFW filter, batch TTS, damage detection, UGC moderation).
  Workspaces at `/tmp/genblaze-feedback-{01..10}-*/`, repo clones at
  `/tmp/genblaze-repo-{01..10}/`. Covered the bulk of the P0-04 → P2-24 range.
- **2026-04-24 — builder+reviewer merge** — gpt-image-2 sample planning round exposed
  P1-05, P1-06, P2-04, P2-05, P2-21, P2-22.
- **2026-04-24 — install-path & worker-eval walkthrough** — new-user install flow plus
  no-key evaluation-worker scenarios. Covered P0-07, P1-01 (expanded), P1-13, P1-14,
  P1-15, P2-01 (expanded), P2-25, P2-26, P3-06 → P3-14.
- **2026-04-24 — app-builder dependency/provenance batch** — production-app authors
  hitting optional-dep import leaks, inline-embed hash semantics, business metadata
  plumbing, and template param rendering. Covered P1-01 (expanded), P1-11 (expanded),
  P1-16, P1-17, P2-16 (expanded), P2-17 (expanded), P2-25 (expanded), P2-27 → P2-32,
  P3-11 (expanded), P3-15 → P3-19. One unverified sub-claim noted in P2-27
  (`RunnableConfig` merge dropping callbacks) — flagged for follow-up.
