<!-- last_verified: 2026-04-24 -->
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

Two feedback corpuses merged this round: a maintainer session covering 0.2.1 regressions,
and a 10-agent sampleapps survey. The dominant themes:

1. **Analysis-shaped workflows are second-class.** The SDK is modeled around *generation*;
   ingest/transcribe/classify/moderate pipelines force throwaway providers, fake
   `data:`/`file:///` URLs, and payload-smuggling through `metadata`. (items P0-04,
   P0-05, P0-06, P1-08, P1-09)
2. **Silent contract narrowing in 0.2.1.** Three cases where documented or previously-
   working surfaces started failing at runtime with misleading errors
   (`from_result`, `GMICloudBase(models=...)`, video slug canonical direction). Needs a
   documented deprecation discipline before the next tranche of fixes. (items P0-01,
   P0-02, P0-03)
3. **Provider coverage gaps block common flows.** No Gemini image, no OpenAI chat or
   Whisper — every transcription/text/chat sample hand-rolls a `BaseProvider`. (items
   P1-05, P1-06)
4. **Introspection and factory ergonomics are thin.** Class-level model catalogs, `b2_sink`
   / `default_tracer` presets, and a `check_models()` helper would collapse boilerplate
   across every sample. (items P2-*)

The `[Unreleased]` CHANGELOG already resolves the StreamEvent typing family (Pydantic
discriminated union + JSON Schemas under `libs/spec/schemas/events/` + TS
`genblaze.d.ts`). Those items are in `### Resolved` below — do not re-open.

## Inbox

_(Empty — all 2026-04-24 feedback triaged into the priority sections below.)_

## P0 — Blocking

| ID | Title | Shape | Evidence | Notes |
|----|-------|-------|----------|-------|
| P0-01 | `from_result()` silently narrowed to lineage-only | **B** | `libs/core/genblaze_core/pipeline/pipeline.py:177` | Was the documented path to hydrate completed steps so `input_from` could reach across runs; now only sets `_parent_run_id`. Receiving step fails with `input_from index 0 is out of range for step 0 (only 0 prior steps completed)` — error blames the wrong site. **Resolution:** either (a) restore hydration, (b) add sibling `hydrate_from(result)`, or (c) raise at `.step(input_from=...)` build-time when the index is unreachable, pointing at the `image=` param pattern. Needs a deprecation window. |
| P0-02 | Video slug canonical direction inverted | **B** | `libs/connectors/gmicloud/genblaze_gmicloud/models/video.py:24` | Registry marks lowercase (`veo3`, `kling-image2video-v2.1-master`, `sora-2-pro`, `luma-ray-2`, `minimax-hailuo-2.3-fast`, etc.) as canonical with PascalCase as deprecated aliases; GMICloud's live `/models` catalog accepts **only** PascalCase for these slugs. `resolve_canonical()` therefore rewrites valid input into 404-producing form, with a `DeprecationWarning` that reads as intentional. **Resolution:** flip direction for confirmed-PascalCase-only slugs + add a CI contract test that pulls `/models` and asserts every canonical in our registry is in the live catalog (see P0-03's conformance suggestion). |
| P0-03 | `GMICloudBase.__init__` drops the documented `models=` kwarg | **F** | `libs/connectors/gmicloud/genblaze_gmicloud/_base.py:90` | `BaseProvider(*, models=None)` exposes it and the docstring tells users to override there; `GMICloudBase` calls `super().__init__()` bare, so `Provider(models=reg)` raises `TypeError`. **Resolution:** one-line fix (add `models: ModelRegistry \| None = None` to the signature, forward to super) **plus** a cross-provider conformance test asserting every `BaseProvider` subclass accepts `models=` without error. |
| P0-04 | No `Pipeline.input(asset_or_path)` — first step must be a generator | **A** | `libs/core/genblaze_core/pipeline/pipeline.py` | Top friction point in the 10-agent survey (hit by 8/10). Forces throwaway `SyncProvider` subclasses (`LocalFileProvider`, `PassthroughProvider`, `MockVideoProvider`) just to seed step 0 with an existing file or URL. **Resolution:** add `Pipeline.input(asset_or_path)` / `Pipeline.from_asset(path)` that hydrates a virtual step -1 so `input_from=[-1]` or implicit first-arg resolution works. Pairs well with P0-05 (analysis StepTypes). |
| P0-05 | SDK is generation-shaped; analysis workflows don't fit | **A** | `libs/core/genblaze_core/models/enums.py:37` | `StepType` = `{GENERATE, UPSCALE, TRANSCODE, MIX, EDIT, CUSTOM}`. No `INGEST, TRANSCRIBE, CLASSIFY, ANALYZE, EXTRACT, MODERATE`. Hit by 7/10 agents. Analysis results get smuggled through `metadata` or written to fake `file:///data:` URLs. **Resolution:** extend `StepType` + introduce `AnalysisProvider` base that returns structured output instead of asset URLs (see P0-06). |
| P0-06 | `Step.output` / `Asset.text` missing — text & JSON are second-class | **A** | `libs/core/genblaze_core/models/step.py:22`, `libs/core/genblaze_core/models/asset.py:76` | `Step` has `assets` (URL outputs) and `metadata` only — no `output: Any` field for structured JSON. `Asset.url: str` is mandatory with no `text` field, so transcripts/summaries/JSON reports get stuffed into `metadata["text"]` or `data:text/plain;base64,...` URLs (sink behavior on data-URIs is undefined). Hit by 4/10 agents. **Resolution:** add `Step.output: dict \| None` **and** either `TextAsset` or `Asset.text: str \| None` (mutually exclusive with `url`). Design needed to pick one — avoid both. |

## P1 — High impact

| ID | Title | Shape | Evidence | Notes |
|----|-------|-------|----------|-------|
| P1-01 | `genblaze_core/testing.py` top-level `import pytest` | **F** | `libs/core/genblaze_core/testing.py:41` | `from genblaze_core.testing import MockProvider` fails with `ModuleNotFoundError: pytest` outside test envs. Confirmed by 3 agents. **Resolution:** move the import under the function/fixture that needs it, or split `MockProvider` into a pytest-free module. |
| P1-02 | `PromptTemplate("literal")` crashes; only kwarg form works | **F** | `libs/core/genblaze_core/models/prompt_template.py:11` | Positional form is shown in README and `examples/batch_with_templates.py` — Pydantic rejects it. Shipped example is broken. **Resolution:** add a `__init__(self, template=None, /, **data)` shim (or `model_validator(mode='before')`) that accepts one positional string. |
| P1-03 | `Pipeline.run(cache=...)` raises TypeError | **D** or **A** | `libs/core/genblaze_core/pipeline/pipeline.py:836` | `cache` is fluent (`.cache(...)`), not a `run` kwarg. Discoverable-API failure. **Resolution:** docs callout in quickstart **or** accept `cache=` as an alias in `run()`. |
| P1-04 | `batch_run` sync path is serial; `max_concurrency` only applies in `abatch_run` | **F** | `libs/core/genblaze_core/pipeline/pipeline.py:1362` | 500-track footgun — the advertised knob is silently ignored. **Resolution:** use a ThreadPoolExecutor bounded by `max_concurrency` in the sync path, or raise at build time if `max_concurrency>1` and caller used sync. |
| P1-05 | No OpenAI chat/Whisper provider in `genblaze-openai` | **A** | `libs/connectors/openai/genblaze_openai/__init__.py` | Exports are `SoraProvider`, `DalleProvider`, `OpenAITTSProvider` only. Every transcription/classification/text sample hand-rolls a `BaseProvider`. `AudioMetadata.word_timings: list[WordTiming]` slot exists but nothing populates it. **Resolution:** ship `WhisperProvider` + `ChatProvider` (or `ResponsesProvider`). Biggest single feature unlock in the survey. |
| P1-06 | No Gemini image provider in `genblaze-google` | **A** | `libs/connectors/google/genblaze_google/__init__.py` | Exports only `VeoProvider`, `ImagenProvider`. Nano Banana / `gemini-*-flash-image` are delivered via `google-genai`, not Imagen API — folding into `ImagenProvider` would be wrong. Caused a Risk-B STOP on a sample build. **Resolution:** new `GeminiImageProvider` (own model registry slice). |
| P1-07 | No built-in retry on 5xx; error message wrapping obscures upstream codes | **A** | `libs/core/genblaze_core/providers/base.py` (`_submit_request`) | GMICloud explicitly returns `"Backend error (400). Please try again."` inside a 500 envelope; SDK raises on first 5xx. `0.2.1` added `unwrap_error_body()` but the outer wrapping format `"GMICloud submit failed (500): {\"error\":\"...\"}"` is unchanged. **Resolution:** (a) add exponential backoff (3 attempts) at the SDK layer for 5xx + `UPSTREAM_TRANSIENT` as a dedicated `ProviderErrorCode`; (b) surface the canonical inner message, not the nested envelope. |
| P1-08 | `ModerationHook.check_prompt` silently skipped when `step.prompt is None` | **F** | `libs/core/genblaze_core/pipeline/pipeline.py:402` | UGC pipelines that feed text through `input_from` or metadata bypass moderation entirely. Security-affecting. **Resolution:** also run moderation against resolved `input_from` text payloads (ties to P0-06 once `Asset.text`/`Step.output` exists). |
| P1-09 | `DalleProvider` allowed-file-roots rejects `file:///tmp/...` on macOS | **F** | `libs/connectors/openai/genblaze_openai/dalle.py` (allowed-roots resolver) | `/tmp` resolves to `/private/var/folders/...` via Darwin symlinks; allowlist doesn't canonicalize. **Resolution:** `Path.resolve()` on both sides of the allowlist check, or normalize via `os.path.realpath`. Ties to P1-10 (P0-11-06 from prior plan, sandboxed `file://` reads). |
| P1-10 | `Pipeline.step()` default `modality=Modality.IMAGE` | **B** | `libs/core/genblaze_core/pipeline/pipeline.py` (`.step()`) | Surprising default for an AV-centric SDK. **Resolution:** make `modality` required, or default based on provider's `get_capabilities()`. Breaking — needs deprecation warning when omitted. |
| P1-11 | `FFmpegTransform` missing core ops + `overlay_text` has no capability preflight | **A** + **F** | `libs/core/genblaze_core/providers/ffmpeg.py` (ops), transform impl | Missing: `trim`, `extract_audio`, `concat`, `split`, `atempo`, `replace_audio`. `overlay_text` silently requires `libfreetype` (macOS homebrew default omits it) — raw ffmpeg exit code 8 surfaces instead of a capability check. **Resolution:** preflight `ffmpeg -filters` once at init for text ops; add the missing ops (each ~15 LOC). |
| P1-12 | B2 env-var names conflict with parent `sampleapps/` standard | **D** or **B** | `libs/connectors/s3/genblaze_s3/backend.py:339` | Genblaze: `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET`. Sampleapps standard: `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`. Every sample either breaks the standard or needs an aliasing shim. **Resolution:** accept both pairs in `for_backblaze(...)` with a precedence doc note, **or** pick one and deprecate the other. Document prominently either way. |

## P2 — Ergonomics & missing primitives

| ID | Title | Shape | Notes |
|----|-------|-------|-------|
| P2-01 | Export `ModelRegistry` from `genblaze_core.__all__` | **A** | Users reach for it through provider-specific paths today (`libs/core/genblaze_core/__init__.py`). |
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
| P2-16 | Pipeline-level idempotency key | **A** | `StepCache` covers step work, not published-output dedup. |
| P2-17 | Webhook SSRF protection: dev-mode allowlist | **A** | `example.test` hostnames are unusable in local testing today. |
| P2-18 | Response-envelope adapter regression test | **F** | `0.2.1` landed `unwrap_error_body()` / envelope adapter but no recorded-payload test against `outcome.media_urls[0].url`. Add a fixture so future envelope changes don't silently break extraction again. |
| P2-19 | `S3StorageBackend.ping()` + typed exception hierarchy | **A** | Samples write `backend.exists("__health_probe__")` inside `try/except Exception`. |
| P2-20 | `StreamEventType` enum / namespace | **A** | `event.type == "pipeline.completed"` works (typing.Literal) but valid values are undocumented — had to spelunk `observability/events.py`. **Note:** `[Unreleased]` made `StreamEvent` a discriminated union — this item is now mostly a docs re-surface. |
| P2-21 | `Pipeline.stream()` handle carries `run_id` and terminal `result` | **A** | Samples hand-roll `_run_store: dict[str, Any]` to correlate the final `PipelineResult` with the originating `run_id`. **Resolution:** `stream = pipeline.stream(...); stream.result` after iteration; `stream.run_id` once streaming starts. |
| P2-22 | Image providers populate `StreamEvent.preview_url` | **A** | Field exists in `events.py` but image providers don't populate it — frontend pokes `step?.assets?.[0]?.key` to render previews as events arrive. |
| P2-23 | `FFmpegCompositor` is video-only | **A** | No image compositor for side-by-side comparisons; hand-rolled per sample. |
| P2-24 | Vision-analysis / classifier providers | **A** | NSFW, OCR, damage detection, auto-tag all hand-rolled. Pairs with P0-05 analysis StepTypes. |

## P3 — Docs & polish

| ID | Title | Notes |
|----|-------|-------|
| P3-01 | `ModelRegistry` API reference | `has` / `known` / `get` / `resolve_canonical` / `prepare_payload` are discoverable only via `dir()`. Add `docs/model-registry.md` (or flesh out existing). |
| P3-02 | Pipeline concurrency is Pipeline-level, not run-level | Quickstart callout. Currently learnable only by reading a sibling sample. |
| P3-03 | `StreamEvent.to_dict()` vs `model_dump_json()` | One-line example in `docs/features/observability.md` after the `[Unreleased]` Pydantic migration. |
| P3-04 | `genblaze-openai` / `genblaze-google` PyPI pages | Must list registered models verbatim and state scope (e.g., "no Whisper in this release", "Imagen only, not Gemini image") so install-time scope mismatches are caught pre-code. |
| P3-05 | Deprecation discipline callout in release notes | `0.2.1` introduced `deprecated_aliases` with `DeprecationWarning` — good — but P0-01/02/03 above will also be breaking when fixed. Commit to one-minor-version minimum deprecation windows with per-release CHANGELOG callouts. |

## Cross-cutting initiatives

These are meta-items implied by multiple rows above. Each deserves its own exec-plan.

- **Catalog sync CI gate** — resolves P0-02 and prevents recurrence. Ship `provider.fetch_catalog()` and a CI contract test that diffs registry canonicals vs. live `/models` at publish time. (drives P0-02, P2-11 stabilization)
- **Analysis pipeline primitives** — the `AnalysisProvider` + `StepType.{INGEST,TRANSCRIBE,CLASSIFY,ANALYZE,MODERATE}` + `Step.output: dict` + `Asset.text` bundle. Single breaking design touching models, Pipeline, and at least one provider (Whisper). (drives P0-04, P0-05, P0-06, P1-05 partially, P2-13 partially, P2-24)
- **Provider-contract conformance suite** — cross-provider tests that every `BaseProvider` subclass accepts `models=`, honors `get_capabilities()`, and retries on 5xx. (drives P0-03, P1-07)
- **Breaking-change deprecation discipline** — documented policy before P0-01, P0-02, P1-10 ship. (drives P3-05)

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
| R-01 | `StreamEvent` is Pydantic discriminated union | `libs/core/genblaze_core/observability/events.py:55` + CHANGELOG `[Unreleased]`. |
| R-02 | JSON Schemas for all 10 stream-event variants | `libs/spec/schemas/events/v1/` (untracked in git status as of 2026-04-24 — stage & commit in the same PR as the CHANGELOG entry). |
| R-03 | TypeScript `StreamEvent` discriminated union | `libs/spec/ts/genblaze.d.ts` updated in `[Unreleased]`. |
| R-04 | `step.completed` event carries `run_id` | Partial: `libs/core/genblaze_core/pipeline/streaming.py:49` now accepts and propagates `run_id`; callers pass `self.run_id` at `:120-123`. Follow-up: raise at build time if a caller forgets. |
| R-05 | Response-envelope `unwrap_error_body()` | Shipped in `0.2.1`. P2-18 above tracks the missing regression test. |

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
