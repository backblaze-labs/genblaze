<!-- last_verified: 2026-04-25 -->
<!-- cleaned: 2026-04-25 -->
# SDK Feedback Tracker

Living inbox for feedback from real users and sample-app builds. Each entry is
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
  know are wrong). This file tracks *external* pain (what users hit).

Priority key:
- **P0** — blocking regression, silent contract break, or friction that 8+ of 10 sample
  builds hit.
- **P1** — high-impact bug or missing primitive with a real workaround cost.
- **P2** — ergonomics / standards drift / additive API gap.
- **P3** — docs, naming, and minor polish.

Resolution shape key: **A** additive (no break), **B** breaking (needs deprecation window),
**D** docs-only, **F** fix-in-place (bug).

## Executive summary (2026-04-24)

Five feedback corpuses merged so far: a maintainer session covering 0.2.1 regressions,
a 10-build sampleapps survey, an install-path / worker-eval walkthrough, an
app-builder batch focused on dependency isolation / provenance semantics / business
workflows, and a second sampleapps survey focused on Backblaze-standard integration
and post-install docs. The dominant themes:

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
4. **Install-time, discoverability & post-install docs — partly closed.** The `genblaze`
   umbrella metapackage (R-07) and PyPI metadata (R-10) shipped in 0.2.3. Still open:
   `genblaze-cli` isn't on PyPI and isn't a dep of the umbrella; README quickstart imports
   a connector (`genblaze_gmicloud`) that isn't in the base install; README links to
   `examples/` / `docs/features/` / `ARCHITECTURE.md` that don't ship in the wheel;
   `dir(genblaze_core)` returns empty because lazy `__getattr__` doesn't implement
   `__dir__`; `import genblaze` re-exports nothing from the umbrella; pytest / urllib3 /
   pyarrow all leak into top-level import paths. (items P1-01, P1-13, P1-16, P1-18, P2-01,
   P3-18)
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
8. **Backblaze-standard integration friction.** Every Backblaze-published sample needs an
   env-var shim for `B2_APPLICATION_KEY` / `B2_BUCKET_NAME` / `B2_ENDPOINT`; the hardcoded
   `b2ai-genblaze/<version>` user-agent base collides with the sampleapps
   `<app-slug>/<version>` convention; `for_backblaze()` preflight spams 403s on
   placeholder creds and breaks offline unit tests. The SDK is supposed to be the easy
   path for B2 — right now it makes the B2 sample standard harder to meet than rolling
   your own boto3. (items P1-12, P1-21, P2-34)

The `[Unreleased]` CHANGELOG already resolves the StreamEvent typing family (Pydantic
discriminated union + JSON Schemas under `libs/spec/schemas/events/` + TS
`genblaze.d.ts`). Those items are in `### Resolved` below — do not re-open.

## Inbox

### 2026-04-25 (batch 2) — sample-app builder hits GMICloud + retry-policy regressions

A second sample-app builder reported 9 items against `genblaze-core` /
`genblaze-gmicloud` 0.2.0 → 0.2.5 and `@genblaze/spec` 0.2.0 → 0.3.2.
Verified in code on 2026-04-25:

- 4 NEW items (filed below as F-2026-04-25-08 → -11; -10 already implemented this turn).
- 2 UPDATE-EXISTING (P0-01 reconfirmed; R-09 expanded).
- 2 ALREADY-RESOLVED (filed in Resolved as R-17, R-18).
- 2 NEEDS-LIVE-API-VERIFY (F-2026-04-25-12, -13 — reporter and current code
  disagree; the new probe at `tools/probe_gmicloud_wire.py` settles them).

The exec-plan extensions for this batch are at:
- [`active/retry-policy-unification.md`](active/retry-policy-unification.md)
  Phase 2 — `RetryPolicy` class + idempotency scaffolding.
- [`active/gmi-registry-reconciliation.md`](active/gmi-registry-reconciliation.md)
  "Wire-conformance probe" section.

**F-2026-04-25-08** — PixVerse `duration` requires string enum; the shared
`_VIDEO_BASE.with_coercers(duration=int)` forces int across all video
models. Evidence: `libs/connectors/gmicloud/genblaze_gmicloud/models/video.py:41`
applies the int coercer universally; `_PIXVERSE` (line 45) inherits from
`_VIDEO_BASE`. **Resolution shape:** F — replace shared int coercer with
per-model `param_schemas={"duration": EnumSchema([...])}` once probe-confirmed
values are known. Pending output of `tools/probe_gmicloud_wire.py`.

**F-2026-04-25-09** — PixVerse `quality` is upstream-required but not
defaulted or enforced by the SDK. Evidence:
`libs/connectors/gmicloud/genblaze_gmicloud/models/video.py:45-47`
(`_PIXVERSE = _VIDEO_BASE.extend("quality")` — allowlist only, no
`param_defaults` or `param_required`); `libs/core/genblaze_core/providers/params.py:134-143`
(`ParamSurface.build()` has no required-param enforcement). Hits 1/1 sample
build that didn't know to pass `quality`. **Resolution shape:** F + A —
either ship `param_defaults={"quality": "720p"}` or extend `ParamSurface`
with a `param_required` set + a build-time validation gate that pairs with
the existing `param_allowlist` enforcement. Already partly tracked in
`active/gmi-registry-reconciliation.md`.

**F-2026-04-25-10** — `RetryPolicy` class promised in CHANGELOG [0.2.5] but
never shipped. **RESOLVED IN THIS RELEASE.** Implementation landed at
`libs/core/genblaze_core/providers/retry.py` (frozen dataclass with seven
knobs and three preset classmethods); wired into `BaseProvider` via the
`retry_policy=` constructor kwarg + `retry_policy` property; cross-provider
conformance test at `tests/conformance/test_provider_contract.py::test_accepts_retry_policy_kwarg`.
27 unit tests at `tests/unit/test_retry_policy.py`. CHANGELOG `[Unreleased]`
carries a `### Corrected` callout for the 0.2.5 overstatement. Move to
Resolved as R-19 once `[Unreleased]` ships.

**F-2026-04-25-11** — No idempotency-key generation on submit retries.
Subset of original Item 9. **Scaffolding shipped in this release**
(`BaseProvider.IDEMPOTENCY_HEADER_NAME` opt-in class attribute +
`_inject_idempotency_header(headers, step)` helper, called via
`RetryPolicy.make_idempotency_key()` with three strategies: `step_id`
default, `uuid_per_attempt`, `none`). Per-provider header rollouts remain
follow-ups — see the rollout-status table in
[`docs/features/retry-policy.md`](../features/retry-policy.md). Resolution
shape: **A**. Status: **partial** — scaffolding ✅, per-provider rollouts ❌.

**F-2026-04-25-12** — Per-model image wire-key translation for GMICloud i2v
(`kling-image2video-v2.1-master`, `wan2.6-i2v`, `pixverse-v5.6-i2v`).
**REPORTER vs CODE DISAGREES.** Reporter claims each wants a different key
(`image` / `img_url` / `image_url`); current registry uses `_VIDEO_BASE`
allowlist (both `image` and `image_url` accepted, no per-model rename) at
`libs/connectors/gmicloud/genblaze_gmicloud/models/video.py:38-43,102,131,134`.
**Action:** run `tools/probe_gmicloud_wire.py` against staging GMICloud
creds; results land at `docs/reference/gmicloud-wire-probe-{date}.{json,md}`.
If reporter is right, ship `param_aliases` per model. If we're right, ask
reporter to upgrade past the version they hit it on.

**F-2026-04-25-13** — GMICloud `/models` may require PascalCase for 7 named
families that 0.2.2 normalized to lowercase (R-06).
**REPORTER vs CODE DISAGREES.** Reporter claims `Kling-Image2Video-V2.1-*`,
`Kling-Text2Video-V2.1-*`, `Veo3`, `Veo3-Fast`, `Sora-2-Pro`, `Luma-Ray-2`,
`Minimax-Hailuo-2.3-Fast` only accept PascalCase live; R-06 (CHANGELOG 0.2.2
L292-299) deliberately rewrote them to lowercase as the live-accepted form.
**Action:** `tools/probe_gmicloud_wire.py --skip-i2v --skip-duration` covers
the slug-case matrix. If reporter is right, partially revert R-06 — declare
PascalCase canonical and lowercase the deprecated alias for the affected
families only (other slugs like Seedance keep lowercase). Per-family flip,
not a global revert. CHANGELOG callout required.

**Updates to existing rows:**

- **P0-01** (`from_result()` narrowed) — **Re-reported 2026-04-25** by the
  second sample-app builder hitting the same misleading
  `input_from index 0 is out of range` error. Status unchanged. Strengthens
  priority for the build-time validation option (c) in the original
  resolution shape.
- **R-09** (5xx transient retry) — promote from "partial" to "fulfilled
  for the originally-claimed scope":
  Retry on 5xx ✅ (`providers/base.py` outer loop + `_retry_phase`;
  `RETRYABLE_ERROR_CODES` includes `SERVER_ERROR` — now sourced from
  `RetryPolicy.retryable_codes` so callers can tune).
  `Retry-After` honored ✅ (`retry.retry_after_from_response`,
  `RetryPolicy.respect_retry_after=True`).
  `StepRetriedEvent` shipped ✅ (`events.py:160-186`, fired via
  `_emit_retry()`).
  Per-provider retry override ✅ (NEW: `Provider(retry_policy=...)` —
  closes F-2026-04-25-10).
  Idempotency-key generation: scaffolding ✅, per-provider rollout ❌
  (tracked at F-2026-04-25-11).

**Items the reporter raised that are already documented or resolved:**

- Item 6 of the report (`step.failed` wire-format change `message` →
  `error`) — documented in CHANGELOG [0.2.3] L247-251 as a breaking
  change. Filed as **R-18** in Resolved below; no SDK action required,
  though future similar breaks should ship a one-minor deprecation window
  emitting both keys to soften the impact for external webhook consumers
  who don't read CHANGELOG.
- Item 7 (`GMICloudBase.__init__` ignored documented `models=`) — fixed in
  0.2.5 (`gmicloud/_base.py:117-129`); cross-provider conformance test at
  `libs/core/tests/conformance/test_provider_contract.py:75-86`. Filed as
  **R-17** in Resolved below.

### 2026-04-25 — external SDK-review critique (uninformed but with UX kernels)

A third-party reviewer asserted Genblaze "has no primitives", "is just a folder",
and "lacks a quickstart" — all refuted by code-walk against the current repo
(`Pipeline`, `Step`, `Run`, `Asset`, `Manifest`, `BaseProvider`, `ModelRegistry`
all present and exercised; quickstart at README + `examples/quickstart.py` +
`examples/quickstart_local.py`; storage first-class via
`S3StorageBackend.for_backblaze()`; 11 connectors; conformance + probe + retry
policy infra all shipping). Surfacing the genuine UX kernels for triage.
README professionalization (positioning, "Why Genblaze" framing, Concepts
hoist, Runtime section, install dedupe) shipped same-day to address
surface-perception issues independently.

- **F-2026-04-25-01** — `Pipeline.run(save=True)` autoresolves an
  `ObjectStorageSink` from env. Critique: `sink=storage` requires three import
  lines before the demo runs. Resolution shape: **A** — boolean opens default
  sink built from `B2_KEY_ID` / `B2_APP_KEY` (or `GENBLAZE_STORAGE_URI`),
  no-op when env absent. Pairs with P2-09 (`b2_sink` preset) — likely subsume
  into the same preset surface.
- **F-2026-04-25-02** — `Pipeline.replay(run_id)` /
  `Pipeline.from_manifest_uri(uri)` programmatic replay. CLI already has
  `genblaze replay`; SDK callers fall back to loading the manifest by hand.
  Resolution shape: **A**. Pairs with P2-16 (idempotency / rehydrate recipe),
  P2-33 (`with_parent`).
- **F-2026-04-25-03** — String-routed model selection:
  `Step.generate(model="openai:sora-2", ...)`. Sugar over typed provider
  classes via the existing entry-point registry. Matches LiteLLM / Vercel AI
  ergonomic; lowers first-touch friction. Resolution shape: **A** — thin
  `ProviderRegistry.resolve("openai:sora-2") -> (ProviderClass, model_id)`
  factory; keep typed providers as the supported surface. Don't replace the
  typed API — additive only.
- **F-2026-04-25-04** — CLI subcommands: `genblaze init` (scaffold
  `pipeline.py` + `.env.example`) and `genblaze run pipeline.py` (import the
  module's `pipeline` symbol, execute it, stream events to terminal).
  Distinct from P1-13 (distributing the existing CLI). Pairs with P3-06
  (offline quickstart promotion) and the "First-30-minutes experience"
  cross-cutting initiative. Resolution shape: **A**.
- **F-2026-04-25-05** — Reference applications under `examples/apps/`: 2–3
  packaged end-to-end apps with their own README + requirements. Candidates:
  text → image → B2-published gallery; long-form audio → transcription →
  searchable summary; agentic refinement loop with quality scoring. Distinct
  from P1-18 (which inlines snippets into the README). Goal: prove
  production-readiness to skimmers. Resolution shape: **A** (docs/examples).
- **F-2026-04-25-06** — `Pipeline.run(evaluate=True)` evaluator hooks
  contract: pluggable quality scorers / output validators that fire after
  each step or at terminal step. Today users wire equivalent logic through
  `on_step_complete`. Resolution shape: **A**, **deferred until concrete user
  demand** — half-built eval is worse than none. Possibly subsumed by the
  analysis-pipeline cross-cutting initiative once `Step.output` lands
  (P0-06).
- **F-2026-04-25-07** — Explicit `Pipeline.branch(condition=fn, then=Step,
  else_=Step)` conditional helper. Today expressible via `on_step_complete` +
  `fail_fast=False` + `input_from`. Resolution shape: **A**, **low
  priority** — only ship if a real example needs it; current patterns cover
  the use cases. Don't preempt for the critique's snippet alone.

**Items the critique demanded that we are explicitly NOT pursuing:**

- Replacing typed provider classes with `Step.generate_text()` /
  `Step.generate_image()` static methods. Discards the `BaseProvider`
  lifecycle, `ProviderCapabilities` typing, and per-provider conformance
  suite. The string-router (F-2026-04-25-03) gives the ergonomic without the
  architectural regression.
- Renaming "embedding" terminology in response to RAG/vector-embedding
  collision concerns — already tracked at P2-13. Single design touchpoint,
  not a critique-driven rewrite.
- Re-architecting around the critic's `pipeline.run(save=True)` /
  `pipeline.replay(run_id="abc123")` as the *only* APIs. The current
  `sink=` / `from_result()` surface stays; the items above are additive
  sugar.

## P0 — Blocking

| ID | Title | Shape | Evidence | Notes |
|----|-------|-------|----------|-------|
| P0-01 | `from_result()` silently narrowed to lineage-only | **B** | `libs/core/genblaze_core/pipeline/pipeline.py:177` | Was the documented path to hydrate completed steps so `input_from` could reach across runs; now only sets `_parent_run_id`. Receiving step fails with `input_from index 0 is out of range for step 0 (only 0 prior steps completed)` — error blames the wrong site. **Resolution:** either (a) restore hydration, (b) add sibling `hydrate_from(result)`, or (c) raise at `.step(input_from=...)` build-time when the index is unreachable, pointing at the `image=` param pattern. Needs a deprecation window. |
| P0-03 | `GMICloudBase.__init__` drops the documented `models=` kwarg | **F** | `libs/connectors/gmicloud/genblaze_gmicloud/_base.py:90` | `BaseProvider(*, models=None)` exposes it and the docstring tells users to override there; `GMICloudBase` calls `super().__init__()` bare, so `Provider(models=reg)` raises `TypeError`. **Resolution:** one-line fix (add `models: ModelRegistry \| None = None` to the signature, forward to super) **plus** a cross-provider conformance test asserting every `BaseProvider` subclass accepts `models=` without error. |
| P0-04 | No `Pipeline.input(asset_or_path)` — first step must be a generator | **A** | `libs/core/genblaze_core/pipeline/pipeline.py` | Top friction point in the sampleapps survey (hit by 8/10 sample builds). Forces throwaway `SyncProvider` subclasses (`LocalFileProvider`, `PassthroughProvider`, `MockVideoProvider`) just to seed step 0 with an existing file or URL. **Resolution:** add `Pipeline.input(asset_or_path)` / `Pipeline.from_asset(path)` that hydrates a virtual step -1 so `input_from=[-1]` or implicit first-arg resolution works. Pairs well with P0-05 (analysis StepTypes). |
| P0-05 | SDK is generation-shaped; analysis workflows don't fit | **A** | `libs/core/genblaze_core/models/enums.py:37` | `StepType` = `{GENERATE, UPSCALE, TRANSCODE, MIX, EDIT, CUSTOM}`. No `INGEST, TRANSCRIBE, CLASSIFY, ANALYZE, EXTRACT, MODERATE`. Hit by 7/10 sample builds. Analysis results get smuggled through `metadata` or written to fake `file:///data:` URLs. **Resolution:** extend `StepType` + introduce `AnalysisProvider` base that returns structured output instead of asset URLs (see P0-06). |
| P0-06 | `Step.output` / `Asset.text` missing — text & JSON are second-class | **A** | `libs/core/genblaze_core/models/step.py:22`, `libs/core/genblaze_core/models/asset.py:76` | `Step` has `assets` (URL outputs) and `metadata` only — no `output: Any` field for structured JSON. `Asset.url: str` is mandatory with no `text` field, so transcripts/summaries/JSON reports get stuffed into `metadata["text"]` or `data:text/plain;base64,...` URLs (sink behavior on data-URIs is undefined). Hit by 4/10 sample builds. **Resolution:** add `Step.output: dict \| None` **and** either `TextAsset` or `Asset.text: str \| None` (mutually exclusive with `url`). Design needed to pick one — avoid both. |

## P1 — High impact

| ID | Title | Shape | Evidence | Notes |
|----|-------|-------|----------|-------|
| P1-01 | `genblaze_core/testing.py` top-level `import pytest` | **F** | `libs/core/genblaze_core/testing.py:41` (also houses `MockVideoProvider`:123 / `MockAudioProvider`:152) | Shipped-broken entry point: `MockProvider` is in `testing.__all__` but `from genblaze_core.testing import MockProvider` fails with `ModuleNotFoundError: pytest` on a fresh `pip install genblaze-core` (pytest is not a runtime dep). `MockVideoProvider` / `MockAudioProvider` share the same breakage. Worker scripts and offline eval runs can't use the documented mocks. Confirmed across four feedback batches. **Resolution (preferred):** lazy-import `pytest` inside `ProviderComplianceTests` only, OR split the three mock classes into a pytest-free module (`genblaze_core.mock`); keep pytest-specific fixtures under `genblaze_core.testing`. Alternative: publish `genblaze-core[testing]` extra and document on the first install page. |
| P1-02 | `PromptTemplate("literal")` crashes; only kwarg form works | **F** | `libs/core/genblaze_core/models/prompt_template.py:11` | Positional form is shown in README and `examples/batch_with_templates.py` — Pydantic rejects it. Shipped example is broken. **Resolution:** add a `__init__(self, template=None, /, **data)` shim (or `model_validator(mode='before')`) that accepts one positional string. |
| P1-03 | `Pipeline.run(cache=...)` raises TypeError | **D** or **A** | `libs/core/genblaze_core/pipeline/pipeline.py:836` | `cache` is fluent (`.cache(...)`), not a `run` kwarg. Discoverable-API failure. **Resolution:** docs callout in quickstart **or** accept `cache=` as an alias in `run()`. |
| P1-04 | `batch_run` sync path is serial; `max_concurrency` only applies in `abatch_run` | **F** | `libs/core/genblaze_core/pipeline/pipeline.py:1362` | 500-track footgun — the advertised knob is silently ignored. **Resolution:** use a ThreadPoolExecutor bounded by `max_concurrency` in the sync path, or raise at build time if `max_concurrency>1` and caller used sync. |
| P1-05 | No Whisper (STT) provider in `genblaze-openai` | **A** | `libs/connectors/openai/genblaze_openai/__init__.py` (exports `SoraProvider`, `DalleProvider`, `OpenAITTSProvider`, `chat` wrappers — no Whisper) | Chat / text-completion coverage shipped in 0.2.4 (see R-08). **STT remains open:** every transcription sample still hand-rolls a `BaseProvider`. `AudioMetadata.word_timings: list[WordTiming]` slot exists but nothing populates it. **Resolution:** ship `WhisperProvider` (sync + async). Ties to P0-05 analysis StepTypes — a transcription step's output is structured JSON, not a media URL. |
| P1-06 | No Gemini image provider in `genblaze-google` | **A** | `libs/connectors/google/genblaze_google/__init__.py` | Exports only `VeoProvider`, `ImagenProvider`. Nano Banana / `gemini-*-flash-image` are delivered via `google-genai`, not Imagen API — folding into `ImagenProvider` would be wrong. Caused a Risk-B STOP on a sample build. **Resolution:** new `GeminiImageProvider` (own model registry slice). |
| P1-08 | `ModerationHook.check_prompt` silently skipped when `step.prompt is None` | **F** | `libs/core/genblaze_core/pipeline/pipeline.py:402` | UGC pipelines that feed text through `input_from` or metadata bypass moderation entirely. Security-affecting. **Resolution:** also run moderation against resolved `input_from` text payloads (ties to P0-06 once `Asset.text`/`Step.output` exists). |
| P1-09 | `DalleProvider` allowed-file-roots rejects `file:///tmp/...` on macOS | **F** | `libs/connectors/openai/genblaze_openai/dalle.py` (allowed-roots resolver) | `/tmp` resolves to `/private/var/folders/...` via Darwin symlinks; allowlist doesn't canonicalize. **Resolution:** `Path.resolve()` on both sides of the allowlist check, or normalize via `os.path.realpath`. Ties to P1-10 (P0-11-06 from prior plan, sandboxed `file://` reads). |
| P1-10 | `Pipeline.step()` default `modality=Modality.IMAGE` | **B** | `libs/core/genblaze_core/pipeline/pipeline.py` (`.step()`) | Surprising default for an AV-centric SDK. **Resolution:** make `modality` required, or default based on provider's `get_capabilities()`. Breaking — needs deprecation warning when omitted. |
| P1-11 | `FFmpegTransform` missing core ops + `overlay_text` has no capability preflight | **A** + **F** | `libs/core/genblaze_core/providers/ffmpeg.py` (ops), transform impl | Missing: `trim`, `extract_audio`, `concat`, `split`, `atempo`, `replace_audio`, **audio mixdown (pre-mux)**, **multi-track audio** (layered music/VO/SFX). `overlay_text` silently requires `libfreetype` (macOS homebrew default omits it) — raw ffmpeg exit code 8 surfaces instead of a capability check. **Resolution:** preflight `ffmpeg -filters` once at init for text ops; add the missing ops (each ~15 LOC). Pairs with P2-23 (image compositor) and P3-19 (`FFmpegCompositor` file-roots + multi-input docs). |
| P1-12 | B2 env-var names conflict with parent `sampleapps/` standard; `B2_ENDPOINT` ignored | **A** + **D** | `libs/connectors/s3/genblaze_s3/backend.py:477-562` — `for_backblaze()` reads `B2_BUCKET`, `B2_REGION`, `B2_KEY_ID`, `B2_APP_KEY`; `B2_ENDPOINT` is not read (endpoint derived from region only) | Genblaze: `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET`. Sampleapps standard: `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`, `B2_ENDPOINT`. Hit by 9/10 sample builds in the latest survey — every Backblaze-published Genblaze sample needs a shim. **Resolution:** accept both pairs as aliases with explicit precedence (`B2_APPLICATION_KEY` ≻ `B2_APP_KEY` when both set, log at INFO), **and** honor an explicit `B2_ENDPOINT` override when provided (falls back to region-derived). Pure additive. |
| P1-13 | `genblaze-cli` advertised in README but not installed by any `pip install genblaze*` path | **D** or **A** | `README.md:93` (`pip install genblaze-cli`), `README.md:365-367` (`genblaze extract/verify/replay`); source at `cli/pyproject.toml` (name = `genblaze-cli`, `0.1.0`, entry point `genblaze = "genblaze_cli.main:cli"`); NOT a dep of `libs/meta/pyproject.toml` (umbrella installs `genblaze-core`+`genblaze-s3` only) | `pip install genblaze-cli` fails; `pip install genblaze` does not pull it as a transitive dep either. Isolated venvs have no `genblaze` executable. Related: `README.md:176,319` quickstart imports `GMICloudVideoProvider` from `genblaze_gmicloud`, which also isn't in the base `pip install genblaze` — users have to `pip install genblaze-gmicloud` separately. **Resolution:** (a) cut `genblaze-cli==0.1.0` to PyPI and add to the meta umbrella's `[project.optional-dependencies].cli` (or to base if the CLI is load-bearing); (b) update README quickstart to either use an installed-by-default provider **or** include the connector install command alongside. Do both — the current state sets two broken first-impression traps. |
| P1-14 | No `LocalFilesystemSink` / `LocalArchiveSink` for offline evaluation | **A** | `libs/core/genblaze_core/storage/sink.py` (only `ObjectStorageSink` is exported); `examples/quickstart_local.py` works around it with manual manifest writes | Real evaluation / CI workflows frequently generate local `file://` assets before any upload. `ObjectStorageSink` is object-storage-only and exposes no `allowed_roots` option for workspace-local files, so workers hand-roll sinks that write assets + sidecars + manifest to disk. **Resolution:** add `LocalFilesystemSink(root, *, allowed_roots, key_strategy)` sibling of `ObjectStorageSink` implementing the same `BaseSink` contract, and document the offline flow (generate → hash → manifest → sidecars → verify) in a `docs/features/local-workflows.md` page. Pairs with P0-04 (`Pipeline.input`) and P1-15 (manifest/sidecar helpers). |
| P1-15 | No `PipelineResult.save_manifest(path)` / `write_sidecars_for_assets()` helpers | **A** | `libs/core/genblaze_core/pipeline/result.py:37` — only has `failed_steps`, `succeeded_steps`, `error_summary`, `save` | Every offline/eval sample reinvents the same ~40 LOC: walk `result.steps`, collect assets, compute SHA-256, write a canonical manifest JSON, emit per-asset `.c2pa.json` sidecars. Sidecar-only is the safer default for hash-sensitive flows (see P1-17, P3-11). **Resolution:** `PipelineResult.save_manifest(path, *, sidecars=True)` that writes the canonical manifest and optional per-asset sidecars in one call. Pairs with P1-14. |
| P1-16 | `urllib3` leaks into `genblaze_core` import path | **F** | `libs/core/genblaze_core/storage/transfer.py:14` (`import urllib3` at module level); re-exported through `storage/sink.py:15` and `storage/__init__.py:10` | `from genblaze_core import ObjectStorageSink` fails in minimal installs that don't ship `urllib3`. Blocks local/offline evaluation flows that never touch an object-storage backend. Parallel to P1-01 (pytest) and P3-18 (pyarrow) — same anti-pattern. **Resolution:** lazy-import `urllib3` inside the transfer function that actually needs it, or pin `urllib3` as a hard dep of `genblaze-core` if transfer is always required. Covered by the "Optional dependency isolation" cross-cutting initiative. |
| P1-17 | Inline manifest embedding can invalidate `Asset.sha256` | **A** / **B** | `libs/core/genblaze_core/media/embedder.py:36`, `libs/core/genblaze_core/models/manifest.py:161-204` (full-embed with redaction already raises `ManifestError`) | Provenance-correctness gap: inline embedding mutates the media bytes **after** `Asset.sha256` is recorded, so the delivered embedded file won't verify against the manifest hash. The full-embed + redaction case is already guarded (raises), but the non-redacted full-embed case silently produces an un-verifiable artifact. The whole SDK value-prop is provenance integrity — this needs a clear story. **Resolution (pick one):** (a) compute and record both `sha256_source` and `sha256_embedded` on the `Asset` when inline-embedding; (b) make sidecar the default for any flow that wants post-delivery verification and raise on inline-embed unless the caller explicitly opts out; (c) rename the field so users understand it's pre-embed. Document the chosen model in `docs/features/provenance.md`. Decision needed before any new embedder method (P2-31) ships. |
| P1-18 | README docs cliff for PyPI consumers | **D** | `README.md` links to `examples/`, `docs/features/*.md`, `ARCHITECTURE.md` — none of these ship in the wheel for any package (verified against `libs/*/MANIFEST.in` / `pyproject.toml` build-include lists) | Hit by 6/10 sample builds. A user who only has `pip install genblaze-core` reads the README on PyPI, clicks through to a recipe, and 404s. Every post-install reference path is broken for them. **Resolution:** (a) pick the 5-6 most-referenced examples (offline quickstart, fan-out, review gate, custom provider, cost-by-tag, large-file generation) and **inline** them into `README.md` as fenced code blocks; (b) ship `examples/` inside the wheel for `genblaze-core` via `[tool.hatch.build.targets.wheel].include`; (c) resolve all cross-links in the published README to absolute GitHub URLs. Pairs with P3-06 (promote offline quickstart) and P3-20 (custom-provider section). |
| P1-19 | `StorageBackend.get(key) -> bytes` has no streaming / range-read alternative | **A** | `libs/core/genblaze_core/storage/base.py:115-117` (`get() -> bytes` only); S3 impl at `libs/connectors/s3/genblaze_s3/backend.py:228-237` mirrors | Generated media (multi-GB video renders, long-form audio) forces whole-object loads into RAM. Hit by 3/10 sample builds. Undocumented escape hatch: `get_url()` + presigned URL into `ffmpeg -i` (works but abstracts-away badly). **Resolution:** add `get_range(key, *, offset, length) -> bytes` and `stream(key) -> Iterator[bytes]` to the abstract interface with S3 implementations; document the presigned-URL escape hatch in the "large-file generation" recipe. Pairs with P1-15 (manifest helpers) and P2-04 (list primitive). |
| P1-20 | No sink-routed byte emission — `SyncProvider.emit_bytes(data, media_type)` | **A** | Providers that generate bytes locally (self-hosted SDXL, custom TTS, ffmpeg transforms) have no way to route output through the `BaseSink` abstraction — each ends up importing `boto3` / `genblaze-s3` directly, which defeats the sink interface | Hit by 3/10 sample builds. Related to P1-14 (LocalFilesystemSink) but distinct: this is about the SyncProvider → Sink byte path, not the sink backend itself. **Resolution:** add `SyncProvider.emit_bytes(data: bytes, *, media_type: str, suggested_name: str \| None = None) -> Asset` that defers to the active sink (creates the key, uploads, returns an `Asset` with the durable URL). Pairs with P1-14, P2-09 (`b2_sink` preset). |
| P1-21 | `S3StorageBackend` user-agent base is hardcoded; no per-caller app slug | **A** | `libs/connectors/s3/genblaze_s3/backend.py:22` (`_USER_AGENT = f"b2ai-genblaze/{__version__}"`); line 58 accepts `user_agent_extra` which appends but can't replace the base | Parent `sampleapps/CLAUDE.md` standard requires every B2 sample to set its user agent to `<app-slug>/<version>` (Backblaze b2-samples convention). Today's append-only model produces `b2ai-genblaze/0.2.3 <app-slug>/<version>` which may satisfy but is unclear. 1/10 sample builds but a hard constraint for every Backblaze-published sample. **Resolution:** expose `S3StorageBackend(user_agent_base=...)` that replaces the hardcoded prefix (defaulting to `b2ai-genblaze/<version>`), keeping `user_agent_extra=` for append-on-top semantics. Document the B2 attribution requirement in the connector README. |

## P2 — Ergonomics & missing primitives

| ID | Title | Shape | Notes |
|----|-------|-------|-------|
| P2-01 | Discoverability: `__all__`, `__dir__`, and umbrella re-exports | **A** | `libs/core/genblaze_core/__init__.py:102-114` (`__all__` is defined but lazy `__getattr__` doesn't implement `__dir__`, so `dir(genblaze_core)` returns only module metadata); `libs/meta/` has no `genblaze/__init__.py` so `import genblaze` gets nothing. Hit by 4/10 sample builds. **Missing from `__all__`:** `ModelRegistry`, `ModelSpec`, `RunnableConfig`. **Resolution:** (a) add missing lazy imports; (b) implement `__dir__()` that returns `tuple(__all__)` so REPL/IDE completion works; (c) create `libs/meta/genblaze/__init__.py` that re-exports the top-level surface from `genblaze_core` (so `from genblaze import Pipeline` works after `pip install genblaze`). |
| P2-02 | `Pipeline.name` readable property | **A** | `self._name` exists (`pipeline.py:139`), just needs `@property`. Useful for logs, tracers, assertions. |
| P2-03 | Class-level `Provider.known_models()` + `ModelRegistry` iter/contains | **A** | `Provider.models` is an instance property — forces `Provider(api_key="dummy").models.known()` just to introspect. Add module-level `SUPPORTED_MODELS` constants too. |
| P2-04 | `S3StorageBackend.list(prefix=..., max_keys=..., continuation_token=...)` | **A** | No public list primitive — "manifest-is-the-DB" samples reach into `_client.list_objects_v2`. Pairs with P2-05 (`FileEntry`). |
| P2-05 | Export `FileEntry` / `ManifestEntry` Pydantic model | **A** | First-class `list()` return type with `key`, `size`, `last_modified`, `content_type`. |
| P2-06 | Parallel fan-out: DAG-aware scheduler or `Pipeline.fan_out(params_list)` | **A** | Today `.step()` with `input_from=[N]` on multiple branches runs sequentially — a benchmark on one sample build measured **3.19s serial vs 0.34s** for an `asyncio.gather` equivalent on the same pipeline. Per-language dubs, N-style-variant image gen, multi-resolution video renders, per-prompt A/B all hit this. Per-stage analysis forces hand-rolled asyncio or N disjoint pipelines with no shared parent run. **Resolution:** (a) detect DAG fan-out in the scheduler (sibling steps with no mutual `input_from` run concurrently), **or** (b) add `Pipeline.fan_out(params_list, *, max_concurrency=N)` as an explicit primitive. Option (a) gives the speedup silently; option (b) makes the intent explicit and pairs with P2-16 (correlation key) for batch variants. |
| P2-07 | `Pipeline.astream_windowed(source, window=...)` | **A** | Live ingestion currently forces N tiny one-shot pipelines. |
| P2-08 | `bulk_ingest(paths, sink, concurrency=N)` helper with progress + resume | **A** | Every analysis sample reinvents the same ThreadPoolExecutor loop around `backend.put()`. |
| P2-09 | `genblaze_core.presets.b2_sink(backend, *, prefix="runs")` + `default_tracer(...)` | **A** | Every sample re-implements the same ~10 LOC (`ObjectStorageSink` + `KeyStrategy.HIERARCHICAL` + `LoggingTracer` + optional `OTelTracer`). |
| P2-10 | `genblaze_core.web.sse.stream_to_sse(pipeline, ...)` | **A** | First-party SSE adapter so samples don't hand-roll `json.dumps(event.to_dict())` per turn. |
| P2-11 | `genblaze_core.check_models(*pairs)` helper | **A** | Every first-run sanity check today requires spelunking `provider.models.has(...)`. Return structured `(accepted, unknown, class_missing)` report. |
| P2-12 | `provider.param_schema(model)` introspection | **A** | "Does `aspect_ratio` work for `gpt-image-2`?" requires a network call today. JSON-schema-ish return would drive form UIs and reject silently-ignored kwargs. |
| P2-13 | `Modality.EMBEDDING` + vector primitives | **A** | "Embed" in Genblaze means C2PA manifest embedding — collides with vector-embedding terminology in every RAG/search sample. Namespace risk; address in an exec-plan, not a one-line add. |
| P2-14 | Long-audio chunking helper | **A** | Whisper's 25MB ceiling hits real podcasts; every transcription app reinvents silence-split → N parallel → timestamp-stitch. |
| P2-15 | `Asset.key` / `Asset.backend_key` accessor | **A** | Custom providers parse keys out of URL strings today. |
| P2-16 | Pipeline-level idempotency + documented resume/rehydrate recipe + per-batch correlation | **A** + **D** | `BaseProvider.resume()` / `aresume()` exist but are undocumented and scoped to worker-restart recovery; `StepCache` covers step-work dedup, not published-output dedup. Real web-app flows (a 4-min Veo render, multi-step image refinement) need to survive page refresh and webhook replays. Batch flows (N variants for a single campaign/SKU/locale) also need a correlation key that survives across the N runs. **Resolution:** (a) `Pipeline.run(idempotency_key=...)` that short-circuits to a cached `PipelineResult` when the same key re-runs; (b) documented "rehydrate a pending job from its run_id" recipe using `resume_step` + `Pipeline.with_parent(run_id)` (see P2-33); (c) `batch_run(correlation_key=...)` plumbed through to `Run.metadata` and the webhook payload. |
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
| P2-33 | `Pipeline.with_parent(run_id)` public API | **A** | `libs/core/genblaze_core/pipeline/pipeline.py:177` — `from_result(result)` requires holding the original `PipelineResult`; only workaround to set lineage from a bare `run_id` (e.g. loaded from a DB row) is `pipe._parent_run_id = run_id` (private) | Generation-iteration flows ("regenerate this image with the newer model", "fix step 2 with different params") typically have only the `run_id` from persistence, not the full `PipelineResult`. Hit by 2/10 sample builds. **Resolution:** add `Pipeline.with_parent(run_id: str) -> Self` as the public lineage-setter; keep `from_result(result)` for the in-memory case. Ties to P0-01 (from_result narrowing) and P2-16 (resume/rehydrate recipe). |
| P2-34 | `for_backblaze(preflight=False)` / lazy-only preflight | **A** | `libs/connectors/s3/genblaze_s3/backend.py:557` — `_ensure_region_verified()` is called when `auto_lifecycle=True` (the default), triggering a blocking HeadBucket on construction. Placeholder creds produce 403 stderr noise; offline unit tests break on network calls at import/init time | Hit by 3/10 sample builds. **Resolution:** add `preflight: bool = True` kwarg on `for_backblaze()` and `S3StorageBackend.__init__`; when `False`, defer the region/lifecycle check to the first real I/O call. Also silence the 403 stderr stream at INFO (`logger.info("preflight skipped — invalid creds will surface on first op")`). Ties to P2-19 (`backend.ping()`) — they're two sides of the same ergonomic. |
| P2-35 | `@dataclass` on a `SyncProvider` subclass silently breaks | **A** or **D** | `@dataclass` overrides `__init__` and won't call `super().__init__()`, so `BaseProvider.__init__` (`providers/base.py:221`) never runs — `self._poll_cache_max_age` / `self._sync_results` remain unset, surfacing later as `AttributeError: '_poll_cache_max_age'` mid-run | Common Python gotcha but the SDK can detect it. Hit by 2/10 sample builds (real time cost). **Resolution:** (a) `__init_subclass__` guard on `BaseProvider` that raises at import time if the subclass is decorated with `@dataclass` and its `__init__` doesn't forward; (b) failing that, a prominent "Don't use `@dataclass` on providers" callout in the provider-authoring guide (P3-20). |
| P2-36 | `per_input_chars` pricing silently returns `cost_usd=None` on chain-input | **F** | `libs/core/genblaze_core/providers/pricing.py:70-79` — line 74 reads `text = ctx.step.prompt or ""`, line 75-76 returns `None` if text is falsy. Chain-input steps (`input_from=[...]` without a prompt) silently cost-account to nothing | Cost-accounting correctness issue, not just docs. Samples that drive TTS / analysis from upstream step output get `cost_usd=None` even though the provider did real work. **Resolution:** fall back to `ctx.step.inputs[0].metadata.get("char_count")` when `step.prompt` is empty, OR emit a `logger.warning("per_input_chars: no prompt and no resolvable upstream char-count; cost will be None")` so the silent zero is visible. Pairs with P0-06 (`Asset.text`/`Step.output`) — a proper analysis asset would have a `char_count` on its metadata. |

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
| P3-20 | README "Write a custom provider" section with a working generative example | 7/10 sample builds wrote `SyncProvider` subclasses (self-hosted SDXL wrappers, custom TTS, ffmpeg-based transforms) by reading source. The pattern works — it just isn't documented. **Resolution:** add a README section showing a minimal generative `SyncProvider` (inputs → `emit_bytes` → return `Asset`), note the `@dataclass` trap (P2-35), and link the provider-authoring guide (P3-10). Unblocks a common extension path without growing the SDK surface. |
| P3-21 | Document `RETRYABLE_ERROR_CODES` and the absence of a `TRANSIENT` alias | `libs/core/genblaze_core/models/enums.py:46-62` — `ProviderErrorCode` members are `TIMEOUT`, `RATE_LIMIT`, `AUTH_FAILURE`, `INVALID_INPUT`, `MODEL_ERROR`, `SERVER_ERROR`, `CONTENT_POLICY`, `UNKNOWN`. `RETRYABLE_ERROR_CODES = frozenset({TIMEOUT, RATE_LIMIT, SERVER_ERROR})` is exported at module level. Users assumed a `TRANSIENT` member exists (it does not) and that `RETRYABLE_ERROR_CODES` was private. **Resolution:** docs page for the error taxonomy + explicit re-export of `RETRYABLE_ERROR_CODES` in `genblaze_core.__all__`. No code change needed beyond the export. |

## Cross-cutting initiatives

These are meta-items implied by multiple rows above. Each deserves its own exec-plan.

- **Catalog sync CI gate** — resolves P0-02 and prevents recurrence. Ship `provider.fetch_catalog()` and a CI contract test that diffs registry canonicals vs. live `/models` at publish time. (drives P0-02, P2-11 stabilization)
- **Analysis pipeline primitives** — the `AnalysisProvider` + `StepType.{INGEST,TRANSCRIBE,CLASSIFY,ANALYZE,MODERATE}` + `Step.output: dict` + `Asset.text` bundle. Single breaking design touching models, Pipeline, and at least one provider (Whisper). (drives P0-04, P0-05, P0-06, P1-05 partially, P2-13 partially, P2-24)
- **Provider-contract conformance suite** — cross-provider tests that every `BaseProvider` subclass accepts `models=`, honors `get_capabilities()`, and retries on 5xx. (drives P0-03, P1-07)
- **Breaking-change deprecation discipline** — documented policy before P0-01, P0-02, P1-10 ship. (drives P3-05)
- **First-30-minutes experience** — CLI availability + offline quickstart + provider matrix + post-install doc fidelity. Install-path (metapackage, PyPI metadata) resolved in 0.2.3. Goal: a new user can `pip install …` and run something end-to-end without credentials in under 5 minutes, and every link the README shows resolves after install. (drives P1-01, P1-13, P1-18, P2-01, P3-06, P3-07, P3-09)
- **Backblaze-sample integration** — env-var aliasing, endpoint override, configurable user-agent base, non-blocking preflight. The B2 SDK path needs to be a net subtraction of boilerplate vs. hand-rolled boto3, not an addition. (drives P1-12, P1-21, P2-34)
- **Generative custom-provider story** — a documented `SyncProvider` extension pattern, the `@dataclass` guard, the `emit_bytes` sink-routing primitive, and the public `emit_progress`. Roughly every other sample build writes a custom provider; the pattern needs to be first-class rather than folklore. (drives P1-20, P2-32, P2-35, P3-10, P3-20)
- **Offline / local-workflows primitives** — `LocalFilesystemSink` + `PipelineResult.save_manifest()` + pytest-free mock provider + fixture-backed media providers + local-workflows doc. Evaluation and CI are a first-class use case, not a testing afterthought. (drives P1-01, P1-14, P1-15, P2-29, P3-08, P3-09, P3-11, P3-13)
- **Optional dependency isolation** — pytest, urllib3, pyarrow all currently break top-level or near-top-level imports in minimal installs. Establish a lazy-import convention and a CI job that installs `genblaze-core` with zero extras and smoke-tests `from genblaze_core import …` for every public symbol. (drives P1-01, P1-16, P3-18)
- **Provenance correctness story** — decide inline-embed vs sidecar semantics, document it, and align `SmartEmbedder` surface + `PipelineResult.embed_all()` + `param_allowlist` strict-mode around it. The SDK's differentiator is provenance integrity; the current semantics for inline embedding undermine it. (drives P1-17, P2-30, P2-31, P3-11, P3-17)
- **Business provenance modeling** — surface `Run.metadata` / `Step.metadata` through the fluent builder, let `PipelineTemplate` substitute step params, and wire batch correlation/idempotency. Workflows that track campaign/SKU/locale/reviewer/job identity currently shove this data into provider `params` or side indexes. (drives P2-16, P2-25, P2-28)
- **GMICloud live-API conformance** — items F-2026-04-25-08, -09, -12, -13 all point to drift between the registered specs and the live request-queue API (param keys, slug case, required params, value enum types). Recommend extending the existing `tools/probe_models.py` contract (commit d600719) with a "live-submit probe" mode that submits a minimal payload against each GMICloud model variant and asserts the error-response shape matches the spec's expected param-name vocabulary. The first iteration of this is `tools/probe_gmicloud_wire.py` (added 2026-04-25) which targets the three immediate disagreements. Generalize it once the pattern proves out. Pairs with the existing **Catalog sync CI gate** initiative — same machinery, different drift dimension. (drives F-2026-04-25-08, -09, -12, -13.)

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
| R-17 | `GMICloudBase.__init__` accepts `models=` kwarg + cross-provider conformance test (was P0-03) | Fixed in 0.2.5 — `libs/connectors/gmicloud/genblaze_gmicloud/_base.py:117-129` accepts `models=` and forwards to `super().__init__()` with comment "closes feedback P0-03". Conformance test `test_accepts_models_kwarg` at `libs/core/tests/conformance/test_provider_contract.py:75-86` parametrically asserts every discovered provider forwards `models=`. (`active/gmi-registry-reconciliation.md` resolution #3.) |
| R-18 | `step.failed` wire-format renamed `message` → `error` (was Item 6 of 2026-04-25 batch 2) | Documented in `CHANGELOG.md` [0.2.3] L247-251 as a breaking wire-format change ("the serialized event no longer carries a `message` key — the failure reason lives on a dedicated `error` field"). No SDK action required. **Lesson for future similar breaks:** ship a one-minor deprecation window emitting both keys, since external webhook / log consumers don't read CHANGELOG. |

## Source log

- **2026-04-24 — maintainer session** — regressions hit while building a cross-pipeline
  lineage sample + GMICloud video catalog audit. Covered P0-01, P0-02, P0-03, P1-07,
  P2-18, P3-05.
- **2026-04-24 — sampleapps survey (10 builds)** — ten independent sample builds covering
  transcription, compare-stream, moderation, B2 gallery, live ingest, per-language dubs,
  NSFW filter, batch TTS, damage detection, and UGC moderation. Workspaces at
  `/tmp/genblaze-feedback-{01..10}-*/`, repo clones at `/tmp/genblaze-repo-{01..10}/`.
  Covered the bulk of the P0-04 → P2-24 range.
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
- **2026-04-24 — Backblaze-sample-integration survey (2nd sampleapps pass)** — focused
  on the B2 sample-standard intersection, post-install docs fidelity, and the custom
  `SyncProvider` extension path. Covered P1-01 (sharpened), P1-12 (expanded with
  `B2_ENDPOINT`), P1-13 (expanded with umbrella + quickstart-connector gap), P1-18
  (new — docs cliff), P1-19 (new — streaming reads), P1-20 (new — `emit_bytes`), P1-21
  (new — user-agent base), P2-01 (expanded with `__dir__` + umbrella re-exports), P2-06
  (expanded with benchmark + DAG option), P2-16 (expanded with rehydrate recipe), P2-33
  → P2-36 (new), P3-20 → P3-21 (new). Several "real bugs" in the input were refuted by
  verification (docstring/API drift claims for `ProviderError`, `ProviderCapabilities`,
  `PipelineResult`, `chat()` exception wrapping) — left out as false alarms.
- **2026-04-25 — external SDK-review critique** — third-party reviewer feedback.
  Most claims (no primitives, no quickstart, no storage layer, no multi-model,
  no observability, no positioning) refuted by code-walk against the current
  repo. Genuine UX kernels filed as F-2026-04-25-01 → F-2026-04-25-07 in the
  Inbox above. Same-day: README professionalization (Concepts hoist, "Why
  Genblaze" framing, Runtime section, install dedupe) shipped to address
  surface-positioning issues independently.
- **2026-04-25 (batch 2) — sample-app builder hits GMICloud + retry-policy
  regressions** — second-batch report against `genblaze-core` /
  `genblaze-gmicloud` 0.2.0 → 0.2.5 and `@genblaze/spec` 0.2.0 → 0.3.2.
  9 items: 4 NEW (filed F-2026-04-25-08 → -11; -10 implemented this turn as
  the `RetryPolicy` class with seven knobs + idempotency-key scaffolding),
  2 UPDATE-EXISTING (P0-01 reconfirmed; R-09 expanded with the shipped /
  remaining split), 2 ALREADY-RESOLVED (R-17 GMICloudBase `models=` kwarg
  fix + cross-provider conformance test, R-18 `step.failed` wire-format
  rename documented in CHANGELOG [0.2.3]), 2 NEEDS-LIVE-API-VERIFY
  (F-2026-04-25-12 per-model image keys, F-2026-04-25-13 slug case for 7
  named families — reporter and current code disagree, settled by the new
  `tools/probe_gmicloud_wire.py`). Exec-plans extended:
  `active/retry-policy-unification.md` Phase 2 covers the policy class +
  idempotency scaffolding; `active/gmi-registry-reconciliation.md`
  "Wire-conformance probe" section covers the GMICloud drift items.
  CHANGELOG `[Unreleased]` carries a `### Corrected` entry for the [0.2.5]
  release-notes overstatement that promised `RetryPolicy` had shipped.
