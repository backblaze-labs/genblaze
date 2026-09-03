# Genblaze — Complete Repository Briefing

> **Purpose of this document.** This is a self-contained teaching brief about the
> `genblaze` repository, written to be handed to an AI assistant that does **not**
> have access to the codebase. Everything here was verified by reading the actual
> source, running the test suites, and executing the CLI on 2026-09-01/02 at commit
> `a60056b` on branch `main`. Line references point at real files.
>
> **How to use it.** Paste or upload this file, then ask questions like "explain the
> canonical hash," "walk me through what happens when I call `.run()`," or "quiz me
> on the provider lifecycle." Where this brief says something is *not* verified, treat
> that as a genuine gap, not modesty.

---

## 1. What Genblaze is, in one paragraph

Genblaze is an **AI pipeline SDK for generative media** — video, image, and audio —
built by Backblaze. It does two things that are hard to do well: it puts **one unified
`Pipeline` API in front of 13 different generation providers** (OpenAI Sora, Google Veo,
Runway, Luma, ElevenLabs, NVIDIA NIM, GMICloud, and more), so swapping providers is a
one-line change instead of a rewrite; and it produces a **canonical, hash-verified
provenance manifest** for every run, which can be embedded directly *inside* the media
file (`.mp4`, `.png`, `.jpg`, `.webp`, `.mp3`, `.wav`, `.flac`, `.aac`) or persisted
alongside it in Backblaze B2 / S3.

**License:** MIT. **Language:** Python 3.11+. **Repo:** `github.com/backblaze-labs/genblaze`.

### The single most important structural fact

**Genblaze is a library, not an application.** There is no server, no daemon, no web UI,
no dashboard, and nothing to "start." `README.md` states it explicitly under *Runtime*:
"Library-only — no daemon, no service to run." You `pip install` it and `import` it into
your own FastAPI handler, AWS Lambda, Cloud Run service, notebook, or script.

This trips up almost everyone on first contact, so it is worth restating: asking "how do
I run the app?" has no answer, because there is no app. The three things you *can* run are
a Python script that uses the SDK, the `genblaze` CLI (which only inspects manifests), and
the test suite.

### Why it exists / when to reach for it

It occupies the space between "call one video API directly" and "run a media pipeline in
production." The four differentiators the README claims:

1. **Provenance by default** — every run yields a deterministic, embeddable manifest.
2. **One pipeline, many providers** — swap Sora → Runway → Veo by changing one line.
3. **Storage is first-class** — `S3StorageBackend.for_backblaze("bucket")` gives durable,
   credential-free asset URLs.
4. **Replayable runs** — the manifest captures enough to reconstruct the run.

The README is also unusually honest about when *not* to use it: if you only need an LLM
chat loop, use the provider's SDK or LangChain; if you're building a UI-driven JS/TS app,
use the Vercel AI SDK; if you don't care about provenance, the provider's SDK is simpler.

---

## 2. Scale and shape of the repo

| Metric | Value |
|---|---|
| Python packages published | **18** (1 core, 13 providers, S3, LangSmith, CLI, umbrella) |
| Core SDK source | ~21,800 lines across 94 files |
| Connector source | ~14,300 lines |
| Test functions | **~2,871** |
| Core test files | 81 |
| Example scripts | 30 |
| Markdown docs | 87 files |
| JSON Schemas | 5 manifest + 12 event schemas |

It's a **monorepo**: one git repository producing many independently-versioned pip packages.

### Directory map

```
genblaze/
├── libs/
│   ├── core/            genblaze-core — the actual SDK (pipeline, models, media, storage)
│   ├── connectors/      one directory per provider (15 dirs incl. s3 + langsmith)
│   ├── spec/            language-neutral wire contract: JSON Schemas + generated TS types
│   └── meta/            the `genblaze` umbrella package (dependency bundle, no code)
├── cli/                 genblaze-cli — Click-based CLI: extract, verify, replay, index
├── examples/            30 runnable scripts; 3 need no API keys
├── docs/
│   ├── features/        26 feature docs — one per core feature (the best reading)
│   ├── guides/          new-provider walkthrough, 0.3 migration
│   ├── reference/       model matrix, pricing recipes
│   └── exec-plans/      active/ + completed/ execution plans, tech-debt tracker
├── tools/               release automation, model probes, batch issue orchestration
├── .github/workflows/   ci.yml, release.yml, security.yml
├── Makefile             install, test, lint, typecheck, ts-types, pre-release …
├── README.md            product overview, install, quickstart, provider matrix
├── ARCHITECTURE.md      system layout, data flows, ~60 "Core Patterns" bullets
├── AGENTS.md            repo conventions + the invariant list (read this early)
├── CLAUDE.md            Claude Code agent config
├── RELEASING.md         release waves, publish pipeline
└── CHANGELOG.md         188 KB — the project's real history
```

### Package versions as of commit `a60056b`

```
genblaze              0.4.5   (umbrella, libs/meta)
genblaze-core         0.3.8
genblaze-cli          0.3.6
genblaze-s3           0.3.6
genblaze-gmicloud     0.3.5
genblaze-google       0.3.4   genblaze-openai      0.3.4   genblaze-replicate  0.3.4
genblaze-decart       0.3.3   genblaze-elevenlabs  0.3.3   genblaze-hume       0.3.3
genblaze-lmnt         0.3.3   genblaze-nvidia      0.3.3   genblaze-runway     0.3.3
genblaze-stability-audio 0.3.3
genblaze-assemblyai   0.3.2   genblaze-langsmith   0.3.2   genblaze-luma       0.3.2
```

**A versioning trap worth internalizing.** A GitHub Release tag like `v0.4.0` names a
CHANGELOG **wave**, not any package's version. Every package versions independently. So
`pip install genblaze==0.4.0` either fails or — worse — silently resolves to an unrelated
umbrella build from a different wave, giving you stale code with no error. Pin the exact
umbrella version from that wave's release notes, and generate a real lockfile
(`pip freeze` / `uv lock`) for reproducibility, since even the umbrella pins *ranges*
(`genblaze-core>=0.3.8,<0.4`), not exact versions.

---

## 3. The data model — the heart of the system

Four Pydantic v2 models in a strict containment hierarchy:

```
Manifest ──contains──> Run ──contains──> Step[] ──contains──> Asset[]
```

All IDs are UUIDs (an explicit invariant — never sequential integers).

### `Manifest` — `libs/core/genblaze_core/models/manifest.py`

```python
schema_version: str          # currently "1.5"
run: Run                     # required — the whole run lives inside
canonical_hash: str          # SHA-256 of the deterministic JSON
manifest_uri: str | None     # where it was persisted, if it was
encryption_scheme: str | None  # reserved for future use
signature: str | None        # reserved — Mode 2 trust (see §8)
transfer_failures: list[str]   # upload problems recorded, not raised
```

### `Run`

```python
run_id: str                  # UUID
tenant_id / project_id       # multi-tenancy
name: str | None
status: RunStatus
steps: list[Step]
parent_run_id: str | None    # lineage — links a refined run to its ancestor
idempotency_key: str | None
created_at / started_at / completed_at
metadata: dict               # user-supplied provenance tags — IS hashed
```

### `Step` — the richest model

```python
step_id / run_id             # UUIDs
provider: str | None         # "openai"
model: str                   # required — "sora-2"
step_type: StepType
model_version / model_hash
modality: Modality
prompt / negative_prompt: str | None
prompt_visibility: PromptVisibility
seed: int | None
params: dict                 # provider-specific knobs
status: StepStatus
inputs: list[Asset]          # what fed in
assets: list[Asset]          # what came out
provider_payload: dict       # raw provider response — NOT hashed (may be sensitive)
retries: int
cost_usd: float | None
error: str | None
error_code: ProviderErrorCode | None
started_at / completed_at
step_index: int | None
metadata: dict               # user tags — IS hashed
```

### `Asset`

```python
asset_id: str                # UUID
url: str                     # required
media_type: str              # required — "video/mp4"
sha256: str | None           # THE integrity anchor
size_bytes / width / height / duration
video: VideoMetadata | None  # codec, frame_rate, resolution, has_audio
audio: AudioMetadata | None  # sample_rate, channels, codec, word_timings
tracks: list[Track] | None   # multi-stream containers
metadata: dict
```

### The enums — `models/enums.py` (short file, worth knowing verbatim)

```python
Modality:          image | video | audio | text
StepStatus:        pending | submitted | processing | succeeded | failed | cancelled
RunStatus:         pending | running | completed | failed | cancelled
PromptVisibility:  public | private | redacted | encrypted
StepType:          generate | upscale | transcode | mix | edit | custom | ingest | import
ProviderErrorCode: timeout | rate_limit | auth_failure | invalid_input |
                   model_error | server_error | content_policy | unknown

RETRYABLE_ERROR_CODES = {timeout, rate_limit, server_error}
```

Note what is *deliberately* absent from the retryable set: `content_policy` is
deterministic given the same prompt, so retrying is pointless; `auth_failure` and
`invalid_input` won't fix themselves either. `INGEST` and `IMPORT` are the non-generative
step types — they have no Provider, because the bytes already exist and the step exists
only to record provenance for bringing them into the system.

---

## 4. The canonical hash — the cleverest part of the design

This is the concept to understand first, because everything else in the provenance story
depends on it.

### How it's computed — `canonical/json.py`

```python
def canonical_json(data):
    normalized = normalize(data)
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))

def canonical_hash(data):
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
```

### The normalization rules — `canonical/_normalize.py`

Determinism comes from aggressively collapsing every representational choice:

- **Dicts** → sorted by key
- **Floats** → rounded to 10 decimal places; `NaN`/`Inf` → `null`
- **Datetimes** → ISO 8601 with `Z`; a **naive datetime raises `TypeError`** (no silent
  timezone guessing)
- **Strings** → Unicode **NFC** normalization (so visually identical strings with
  different codepoint sequences hash the same)
- **Enums** → `.value`; **UUIDs** → `str()`; **Pydantic models** → `model_dump()`
- **Unsupported types** → raise `TypeError`, explicitly to prevent silent non-determinism
- **Depth cap of 100** → raises `ManifestError`, because `step.params` is free-form
  caller data and unguarded recursion would crash with an uncaught `RecursionError`
  around depth ~500

### What is EXCLUDED from the hash — and why this is the key insight

`models/manifest.py:22` onward defines three exclusion sets:

```python
_STEP_HASH_EXCLUDE = {step_id, run_id, status, error, error_code, retries,
                      cost_usd, started_at, completed_at, provider_payload, step_index}
_RUN_HASH_EXCLUDE  = {run_id, status, created_at, started_at, completed_at,
                      idempotency_key, parent_run_id}
_ASSET_HASH_EXCLUDE = {asset_id, url}
```

Excluded because they are **non-deterministic** (random UUIDs, timestamps), **operational**
(status, retries, cost), or **potentially sensitive** (`error` messages, `provider_payload`).
`url` is excluded because it's a transport hint that varies across re-uploads, presigning,
and CDN→durable rewrites — asset identity is `sha256 + media_type + size_bytes`.

**Deliberately INCLUDED:** `Step.metadata` and `Run.metadata`, because those are
user-supplied provenance tags (project labels, lineage annotations). The source comments
say runtime metrics belong in `provider_payload`, not `metadata`.

### The consequence — verified experimentally

Running `examples/quickstart_local.py` on two different occasions produced **byte-identical
hashes** (`42c451695e3aa766bf5945dffc7aa384ac4d6649b692c21589c5af101863fae8`) despite
completely different `run_id`s and `created_at` timestamps.

So the hash answers **"were these the same inputs?"** — *not* "was this the same execution."
Same prompt + model + params + seed on a different machine a year later → same hash. That
is what makes it usable for CI drift detection, regression testing, and replay validation.

`_ASSET_HASH_EXCLUDE` also carries a forward-looking note: in schema 1.6,
`_strip_asset_for_hash()` will keep a `url_only_unverified` marker so that unhashed
assets cannot collapse to the same canonical payload.

**This is a protected invariant.** `AGENTS.md` states: "Canonical JSON hashing must remain
deterministic — never change key sort order or float normalization." Changing it silently
invalidates every manifest ever written.

---

## 5. The Pipeline API — `pipeline/pipeline.py` (2,980 lines, the biggest file)

`Pipeline` implements `Runnable[None, PipelineResult]`. The canonical shape:

```python
result = (
    Pipeline("my-pipeline")
    .step(provider, model="...", prompt="...", modality=Modality.VIDEO)
    .run(sink=storage, timeout=600)
)
```

### `Pipeline(...)` constructor

```python
Pipeline(
    name=None, tenant_id=None, *, project_id=None,
    chain=False,             # feed each step's output into the next
    structured_log=False,    # legacy → maps to LoggingTracer
    max_concurrency=None,    # must be None or >= 1
    moderation=None,         # ModerationHook
    tracer=None,             # explicit tracer wins over structured_log
    preflight=True,          # model validation before any wire call
)
```

### `.step(...)`

```python
.step(
    provider,                      # a BaseProvider instance — TypeError if not
    *, model,                      # required
    prompt=None,                   # str | PromptTemplate | None
    modality=Modality.IMAGE,
    step_type=StepType.GENERATE,
    fallback_models=None,          # retried on MODEL_ERROR
    input_from=None,               # fan-in: route prior step outputs by index
    external_inputs=None,          # caller-held Assets (mutually excl. w/ input_from)
    expected_duration_sec=None,    # ETA hint echoed on step.started for progress UIs
    metadata=None,                 # user tags; raises on reserved-key collision
    prompt_visibility=PUBLIC,
    params=None,                   # dict form
    **extra_params,                # kwarg form; top-level kwarg wins on conflict
)
```

Design touches worth noticing: passing a bare `chat()` function instead of a
`BaseProvider` raises `TypeError` immediately at the mistake rather than a bare
`AttributeError` later at `run()` (issue #224). And `metadata` / `prompt_visibility` route
to dedicated `Step` fields — a reserved-name guard raises if you try to smuggle them
through `params={}`.

### The runners — seven ways to execute

| Method | Behavior |
|---|---|
| `.run()` | synchronous |
| `.arun()` | async; with `chain=False` steps run **concurrently** |
| `.stream()` | sync generator of progress events |
| `.astream()` | async event stream (typed events — websocket-friendly) |
| `.batch_run()` | multi-prompt; **always sequential** |
| `.abatch_run()` | multi-prompt, semaphore-bounded concurrency |
| `.invoke()` / `.ainvoke()` | the `Runnable` protocol surface |

`batch_run` validates `max_concurrency` but it is **inert** — it warns once if passed
explicitly, because batch clones share provider/sink instances that aren't guaranteed
thread-safe.

### `.run()` parameters

```python
.run(
    sink=None,               # ObjectStorageSink / ParquetSink / WebhookSink
    fail_fast=True,          # stop on first failed step
    raise_on_failure=None,   # see the big warning below
    timeout=None,            # per-step
    max_retries=None,        # per-step
    on_progress=None,
    progress=None,           # spinner; None = auto-enable on a TTY
    pipeline_timeout=None,   # end-to-end wall clock
    on_step_complete=None,   # StepCompleteEvent callback
    on_retry=None,           # StepRetriedEvent callback
)
```

### ⚠️ The single biggest behavioral gotcha: failures don't raise

**In 0.3.x, `Pipeline.run()` does not raise when a step fails.** It records the failure and
returns a `PipelineResult`. Evidence, all consistent:

- `Step` carries `status`, `error`, `error_code` fields; a failed step is marked
  `status="failed"` and the run continues.
- `Manifest.transfer_failures` is a list — upload problems get written down, not raised.
- `PipelineResult.error_summary()` exists precisely *because* errors are aggregated for
  later reading.
- `raise_on_failure=None` (the default) emits a `DeprecationWarning` announcing that
  **0.4.0 will flip the default to raising**, and behaves like `False` today.

This is a defensible design for media pipelines: if step 4 of a 6-step, ten-minute,
real-money run fails, an exception would vaporize the four steps that already succeeded.
But the practical consequence is sharp:

> **`.run()` returning without an exception does not mean the work succeeded.** Check
> `result.run.steps[*].status`, call `error_summary()`, or pass `raise_on_failure=True`.

### Other pipeline features

- **Fan-in** — `input_from=[0, 2]` routes specific prior steps' outputs into a later step
  (the audio/video mux pattern). Dependencies must point at *succeeded* steps with assets;
  a missing producer fails the consumer with `INVALID_INPUT` **before** the provider is
  invoked.
- **Chain safety** — in `chain=True`, a failed step clears `prev_assets` so later steps
  receive empty inputs rather than stale output.
- **`from_result(v1)`** — links a new run to a previous one via `parent_run_id`.
- **`.metadata(**kwargs)`** — run-scoped metadata, additive across calls.
- **`on_submit` callback** — fires after `submit()` with `(step_id, prediction_id)` for
  crash-recovery checkpointing.
- **`PipelineTemplate`** — serialize a pipeline to JSON; `instantiate(variables=...)`
  substitutes `{placeholder}` in prompts and in nested param values.
- **`StepCache`** — cache keyed on the canonical params.
- **Credential guard** — `_reject_credentials_in_params()` scans params and refuses
  anything that looks like a leaked key.

---

## 6. The provider system — `providers/base.py` (2,138 lines)

### The three-method lifecycle (a hard invariant)

Every provider implements exactly this, per `AGENTS.md`: "Provider adapters must implement
`submit/poll/fetch_output` — no exceptions."

```python
class BaseProvider(Runnable[Step, Step]):
    @abstractmethod
    def submit(self, step, config=None) -> Any:        # → prediction_id
    @abstractmethod
    def poll(self, prediction_id, config=None) -> bool:  # → done?
    @abstractmethod
    def fetch_output(self, prediction_id, step) -> Step  # → Step with assets

    def poll_progress(self, prediction_id) -> dict | None   # optional
    def normalize_params(self, ...)                          # standard → native names
```

`SyncProvider` is the convenience base for APIs that return results immediately — you
implement one method and the base class synthesizes the polling lifecycle. Most connectors
use it; `BaseProvider` is for genuinely async, job-queue APIs.

**This uniformity is why provider swapping works.** The `Pipeline` only ever talks to
these three methods.

### Model registry and catalog routing

Rather than shipping hardcoded slug lists that rot, connectors ship **pattern-keyed
`ModelFamily` rules**: a regex, a `spec_template`, and optionally a `FamilyProbe`. Any new
provider slug matching an existing family pattern works the day it ships upstream, with no
SDK release.

`validate_model()` returns a typed `ValidationResult`:

| Outcome | Meaning |
|---|---|
| `OK_AUTHORITATIVE` | user-registered or confirmed by native discovery |
| `OK_PROVISIONAL` | family-matched, but liveness unverifiable |
| `UNKNOWN_PERMISSIVE` | no family match; permissive fallback applies |
| `NOT_FOUND` | discovery says absent, or a probe returned DEAD |

Providers declare a `DiscoverySupport` tier — `NATIVE` (real catalog API), `PARTIAL`, or
`NONE` — so the SDK is **honest about what it can actually verify**. A `NONE`-tier provider
can never return `OK_AUTHORITATIVE` from a family match alone.

`Pipeline.preflight()` gates on this. `NOT_FOUND` raises **before any wire call**;
`OK_PROVISIONAL` and `UNKNOWN_PERMISSIVE` emit one WARN per `(provider, slug)` per Pipeline
instance. This is what catches a typo'd or retired model slug before you pay for a
generation. The log line looks like:

```
preflight.unknown step=0 provider=mock model=mock-v1 — no family matched; permissive fallback applies
```

That is **not an error** — it's the SDK saying "I can't vouch for this model, proceeding anyway."

### Pricing — user-registered, zero shipped prices

As of 0.3.0 the SDK ships **no hardcoded prices at all**; `ModelRegistry(defaults={...})`
is no longer accepted. You register a `PricingStrategy` per slug or per family. The
composable strategies in `providers/pricing.py`:

```python
per_unit(rate)                    per_input_chars(rate, per=1000)
per_output_second(rate)           per_response_metric(extract_fn)
tiered(...)                       bucketed_by_duration(...)
by_param(...)                     by_model_and_param(...)
first_match(*strategies)          # try each in order
```

Unknown models pass through with `cost_usd=None` until you register pricing. Rate sheets
live in `docs/reference/pricing-recipes.md`.

```python
reg = DalleProvider.models_default().fork()
reg.register_pricing("dall-e-3", per_unit(0.040))
reg.register(ModelSpec(model_id="gpt-image-3-preview", pricing=per_unit(0.20)))
provider = DalleProvider(models=reg)
```

### The 13 providers and their concrete classes

| Package | Classes | Video | Image | Audio | Chat |
|---|---|---|---|---|---|
| `genblaze-gmicloud` | `GMICloudVideoProvider`, `GMICloudImageProvider`, `GMICloudAudioProvider`, `GMICloudBase` | Seedance, Kling, Veo, Sora, Wan | Seedream, FLUX, Gemini | ElevenLabs, MiniMax | Llama, DeepSeek, Qwen |
| `genblaze-nvidia` | `NvidiaVideoProvider`, `NvidiaImageProvider`, `NvidiaAudioProvider`, `NvidiaChatProvider`, `NvidiaClient` | Cosmos 1.0/2.0 | SDXL, SD 3.5, FLUX.1/.2 | Fugatto, Riva, Maxine | Nemotron, Llama, Mistral, Qwen, Phi |
| `genblaze-openai` | `SoraProvider`, `DalleProvider`, `OpenAITTSProvider` | Sora | DALL-E / gpt-image (2/1.5/1/1-mini) + edits | TTS | GPT-4o/4.1/o-series |
| `genblaze-google` | `VeoProvider`, `ImagenProvider`, `GeminiImageProvider`, `GoogleClientMixin` | Veo | Imagen | — | Gemini 1.5/2.0/2.5 |
| `genblaze-runway` | `RunwayProvider` | Gen-4 Turbo | — | — | — |
| `genblaze-luma` | `LumaProvider` | Dream Machine | — | — | — |
| `genblaze-decart` | `DecartVideoProvider`, `DecartImageProvider` | Lucy | Lucy | — | — |
| `genblaze-replicate` | `ReplicateProvider` | — | Flux, SDXL, … | — | — |
| `genblaze-elevenlabs` | `ElevenLabsTTSProvider`, `ElevenLabsSFXProvider` | — | — | TTS + SFX | — |
| `genblaze-stability-audio` | `StabilityAudioProvider` | — | — | Stable Audio (music) | — |
| `genblaze-lmnt` | `LMNTProvider` | — | — | fast TTS | — |
| `genblaze-hume` | `HumeTTSProvider` | — | — | Octave TTS | — |
| `genblaze-assemblyai` | `AssemblyAIProvider` | — | — | **speech→text** | — |

Two notes. **AssemblyAI runs the matrix backwards**: it *consumes* an audio URL and
*produces* a hash-verified TEXT transcript asset with word-level timings, composable into
pipelines like any other step. And the **Chat column is not a Pipeline citizen** — `chat()`
is a standalone callable, a convenience for driving media steps from an LLM. Passing it to
`.step()` raises `TypeError`.

Plus two non-provider connectors: `genblaze-s3` (`S3StorageBackend`,
`AsyncS3StorageBackend`, `PresignedURL`, `Encryption`) and `genblaze-langsmith`
(`LangSmithTracer`).

All provider SDKs are **lazily imported** — no runtime dependency unless the connector is
actually used.

### Built-in non-network providers

- `FFmpegCompositor` — muxes video + audio into MP4 via an ffmpeg subprocess
- `FFmpegTransform` — resize, crop, overlay_text, audio_normalize, format conversion
- `MockProvider`, `MockVideoProvider`, `MockAudioProvider` — test doubles (`mocks.py`)
- `ProviderComplianceTests` — the harness new connectors must pass (`testing.py`)

---

## 7. Storage, sinks, media, and observability

### Storage — `ObjectStorageSink` + `S3StorageBackend`

```python
storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),   # reads B2_KEY_ID / B2_APP_KEY
    key_strategy=KeyStrategy.HIERARCHICAL,
    parquet_sink=ParquetSink("data/"),             # optional: cloud + local analytics
)
```

Backblaze B2 is the recommended default (one-liner, credential-free durable URLs that
never expire). AWS S3, Cloudflare R2, and MinIO work through the generic constructor with
an explicit `endpoint_url`.

Two bucket layouts:

```
HIERARCHICAL (run-grouped)                CONTENT_ADDRESSABLE (deduped)
{prefix}/runs/{tenant}/{date}/{run_id}/   {prefix}/assets/{sha[:2]}/{sha[2:4]}/{sha}.ext
    manifest.json                         {prefix}/manifests/{run_id}.json
    assets/{asset_id}.mp4
```

Implementation notes: assets upload **concurrently** via `ThreadPoolExecutor`
(`max_upload_workers`); `AssetTransfer` streams large files to disk through
`SpooledTemporaryFile` instead of buffering in RAM.

> **Sink lifecycle gotcha.** Run-scoped sinks like `ObjectStorageSink` are closed
> automatically in a `finally` block when the run ends — **the sink is spent afterward**.
> Construct a fresh one per run. Fire-and-forget sinks like `WebhookSink` opt out
> (`_close_with_run = False`) and stay reusable.

### The three sinks

| Sink | Purpose |
|---|---|
| `ObjectStorageSink` | B2 / S3 / R2 / MinIO — assets + manifest, URL rewriting |
| `ParquetSink` | three tables (`runs/`, `steps/`, `assets/`), partitioned by `dt=/tenant_id=/modality=/provider=`, idempotent on `run_id` |
| `WebhookSink` | fire-and-forget HTTP status events on a background thread |

### Media embedding — `media/`

Handlers: `PngHandler`, `JpegHandler`, `WebpHandler`, `Mp4Handler`, plus `mp3`, `wav`,
`flac`, `aac` modules and a `SidecarHandler`. `SmartEmbedder` picks by type;
`get_handler()` / `sniff_mime()` / `guess_mime()` help.

Per-format embed mechanism: **PNG** iTXt chunk · **JPEG/WebP** XMP · **MP4** UUID box ·
**MP3** ID3v2 TXXX · **WAV** LIST/INFO · or a sidecar JSON.

Details that show real engineering: WebP **lossless preservation** (detects VP8L and keeps
it); MP4 **seek-based I/O** for 500 MB–2 GB files; **atomic writes** on every path
(temp file + `os.replace`), so a crash mid-embed leaves the original intact.

### Observability

`Tracer` implementations: `NoOpTracer` (default), `LoggingTracer`, `OTelTracer`
(starts real OpenTelemetry spans when the SDK is installed), `CompositeTracer`, and
`LangSmithTracer` from the separate connector. Plus `StructuredLogger`, `StepSpan`, and 12
JSON-schema'd event types (`pipeline.started`, `step.progress`, `step.retried`,
`agent.iteration.evaluated`, …).

**This is what replaces the terminal in production.** In a Lambda or FastAPI handler there
is no stdout to read; you attach a tracer and the same events flow to your logs or
observability stack.

### Security surfaces

- `check_ssrf()` in `_utils.py` blocks private/loopback IPs — used by **both**
  `AssetTransfer` and `WebhookNotifier`
- Webhooks are HTTPS-only, validated at construction, DNS-resolved against private ranges
  on first dispatch
- `validate_chain_input_url()` checks chain input URLs before forwarding to external APIs
  (allows only `file://` and `https://`)
- `EmbedPolicy` — prompt redaction, param stripping, pointer-mode sidecars
- API tokens are **never** stored in manifests (trust-boundary invariant)
- `providers/pattern_safety.py` (956 lines) — ReDoS guards on the family regexes
- `.github/workflows/security.yml` — dedicated security CI

### `AgentLoop` — `agents/loop.py`

Iterative refinement: generate → evaluate → feed critique into the next prompt → repeat.
`Evaluator` / `CallableEvaluator` / `ThresholdEvaluator` produce an `EvaluationResult`
(`score`, `passed`, `feedback`). **Each iteration is its own manifest**, chained by
`parent_run_id`:

```
iter 0: run_id=8c88b348... parent=(root)     score=0.30  feedback='try warmer lighting'
iter 1: run_id=7cc89034... parent=8c88b348...  score=0.60
iter 2: run_id=76724622... parent=7cc89034...  score=0.90  passed=True
```

Note the interaction with §4: `parent_run_id` is in `_RUN_HASH_EXCLUDE`, so lineage is
*recorded* but deliberately kept *out of the hash* — a refined run's identity still
depends only on its own inputs.

---

## 8. Trust modes — what the manifest does and does not prove

`docs/features/trust-modes.md` is the most intellectually honest document in the repo.
Read it early; it prevents overclaiming.

### Mode 1 — Integrity (**the only mode that ships today**)

**Proves:** the manifest content hasn't changed since it was written (the hash recomputes);
the manifest commits to each output asset's `sha256`, so a caller can prove stored bytes
are unchanged by re-hashing them; and the run is reproducible — same inputs, same hash.

**Does NOT prove:**
- Byte integrity for **URL-only** assets. `verify()` returns `False` until every output
  asset has `sha256` populated (typically via `ObjectStorageSink`).
- **That a specific party produced it.** Anyone with the SDK can build a self-consistent
  manifest from arbitrary inputs.
- **Resistance to a determined re-embedder.** A tamperer can modify the asset, recompute
  the manifest, re-embed, and produce something that verifies against itself.

**Rely on it for:** "did *my* pipeline produce this?" — internal audit trails, replay
validation, storage-corruption detection, CI drift detection. Any context where the
manifest reaches the verifier through a **trusted channel** (your own bucket, your own DB).

### Mode 2 — Authenticated integrity (**roadmap**)

Mode 1 + "only the holder of a specific signing key could have produced this." Planned as
a pluggable `Signer`/`Verifier` with an Ed25519 default, bring-your-own-key, no PKI. The
`signature` and `encryption_scheme` fields already exist on `Manifest` and are **excluded
from the canonical hash**, so adding signing later is non-breaking. Not implemented.

### Mode 3 — Standards-verifiable / C2PA (**roadmap, opt-in**)

### The verification API

```python
manifest.verify()        # hash + output sha256 coverage + asset metadata in-spec
manifest.verify_hash()   # hash only — migration/read path
```

Neither default path fetches asset URLs or hashes the post-embed container file.

### ⚠️ The asset-binding caveat — the one result that looks like a bug

`asset.sha256` is the hash of the asset bytes **before** embedding. Embedding *modifies the
file* (PNG inserts an iTXt chunk, MP4 appends a UUID box), so the on-disk file's SHA-256
after embed will **not** match `asset.sha256`.

This was confirmed experimentally: `genblaze verify --fetch` on a locally embedded PNG
**fails by design**, reporting a sha256 mismatch, because `asset.url` points at the same
file that was just modified.

Correct approaches, per `docs/features/media-embedding.md:56`:
1. Hash the **upstream artifact** (the original asset in B2/S3 before embedding), or
2. Extract the manifest, strip the embed region per format, and re-hash the remainder.

---

## 9. The CLI — `cli/genblaze_cli/`

Click-based, four commands, installed as the `genblaze` entry point. Verified version:
`genblaze-cli, version 0.3.6`.

```
genblaze extract <file>          Extract and display the manifest from a media file
genblaze verify  <file>          Verify an embedded, sidecar, or standalone manifest
genblaze replay  <manifest.json> Re-execute a pipeline from a manifest
genblaze index   <manifest.json> Write manifest data to a Parquet sink
```

### `verify` flags

```
--hash-only               Only verify canonical_hash; skip sha256 + metadata checks
                          (mutually exclusive with --fetch)
--fetch                   Also download each output asset and compare bytes to sha256
                          (mutually exclusive with --hash-only)
--allowed-root DIRECTORY  With --fetch: additionally trust file:// assets under this
                          directory (repeatable). By default only system temp dirs are
                          allowed, because pipelines run with output_dir= write elsewhere.
```

Real output, and note how carefully it's worded:

```
OK: manifest hash verified; all output assets declare sha256 and carry in-spec metadata.
Asset bytes were not fetched or compared; add --fetch to verify the media itself.
```

It verified the **record**, not the **pixels** — and says so.

### `replay` redacts by default

```
Prompt: [redacted — visibility=public; pass --show-prompts to reveal public prompts]
...
Dry run — no steps executed. Use --no-dry-run to execute.
```

Prompts are treated as potentially sensitive, so you opt in even at `visibility=public`.
`--no-dry-run` actually re-executes against the live provider.

### ⚠️ The CLI's scope is narrow

The CLI is a **receipt reader**. It inspects manifests and media files. It does **not** run
pipelines (except `replay --no-dry-run`), never calls a provider, and knows nothing about a
generation that failed — a failed run may have no manifest for it to read at all. Errors
during development surface in **your script's own output**, not here.

---

## 10. Development workflow

### Setup (verified working)

```bash
git clone https://github.com/backblaze-labs/genblaze.git && cd genblaze
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv --python 3.12 .venv
make install-dev        # editable-installs all 18 packages with [dev] extras
make test               # verify setup
```

The venv matters on macOS: Homebrew's Python is externally managed and rejects a bare
`pip install`. Without the venv active you get
`ModuleNotFoundError: No module named 'genblaze_core'`.

### Make targets

```
make install / install-dev     editable install (all packages)
make test                      full suite — core, 15 connectors, cli, meta, tools
make lint                      ruff lint + format check
make fmt                       auto-format
make typecheck                 mypy
make coverage                  coverage report (70% minimum)
make deptry                    dependency hygiene
make ts-types                  regenerate libs/spec/ts/genblaze.d.ts from JSON Schemas
make ts-types-check            fail on TS-type drift
make pypi-metadata-check       PyPI metadata validation
make pypi-pin-parity           cross-package pin consistency
make release-smoke             import smoke test
make pre-release               lint + typecheck + ts-types-check + metadata + pins + test + smoke
make post-release VERSION=…    post-publish tasks
```

Faster loops than the full suite:

```bash
cd libs/core && pytest tests/unit/<file>.py -v      # one file
/test-package <name>                                # one package (Claude Code skill)
/test-package changed                               # only changed packages
```

### Verified test results (2026-09-01)

```
libs/core : 1814 passed, 22 skipped, 269 warnings in 59.75s
cli       :   65 passed in 2.03s
```

Repo-wide there are ~2,871 test functions. **The test suite is effectively the SDK's
interface** — for a library with no UI, the tests are the executable specification, and a
failing test name localizes a problem faster than any doc.

### CI — `.github/workflows/ci.yml`

Jobs: `lint`, `ts-types`, `typecheck`, `deptry`, `build` (twine-checks every package),
`test` (core with coverage, then connectors, CLI, release tooling).

**A repo-specific CI policy worth knowing:** Dependabot PRs run **no CI**. Every job in
`ci.yml` and `security.yml` is guarded by
`if: ${{ github.actor != 'dependabot[bot]' && github.event.pull_request.user.login != 'dependabot[bot]' }}`.
Any new job added to a `pull_request`-triggered workflow must copy that guard, or Dependabot
PRs start running CI again. `release.yml` has no `pull_request` trigger, so it needs none.

### The invariants — from `AGENTS.md`

1. All changes must pass `make test` before PR
2. **Canonical JSON hashing must remain deterministic** — never change key sort order or
   float normalization
3. `Manifest.canonical_hash` must always verify against re-serialized content
4. Provider adapters must implement `submit/poll/fetch_output` — no exceptions
5. All IDs are UUIDs — never sequential integers
6. `EmbedPolicy` must be respected in **all** embedding paths
7. Pydantic v2 only — no v1 compatibility layer
8. **Docs must be updated in the same PR as code changes**
9. Python 3.11+

Plus a schema rule: change a Pydantic model in `libs/core/genblaze_core/models/` and you
must update the matching JSON Schema in `libs/spec/schemas/manifest/v1/` and run
`make ts-types` — CI's `ts-types` job fails the PR otherwise. `test_spec_conformance.py`
enforces Pydantic↔Schema agreement, with the **JSON Schemas as authoritative**.

### Release process

Tag names follow the **CHANGELOG wave header** (`v0.3.0`), not any single package's
version. `release.yml` triggers on GitHub Release creation; `workflow_dispatch` runs a
TestPyPI dry-run. Run `make pre-release` and read `RELEASING.md` before tagging;
`make post-release VERSION=<umbrella-version>` after.

### Planning convention

Execution plans live in `docs/exec-plans/active/` and move to `completed/` when done. Plans
are **required** for multi-file changes, new features, and refactors. There are currently
25 active and 30 completed plans, plus `tech-debt-tracker.md` — a genuinely useful window
into what the maintainers consider unfinished.

---

## 11. How to actually run things

### Zero API keys (start here)

```bash
source .venv/bin/activate
python examples/quickstart_local.py    # build + verify a manifest, no network
python examples/streaming_local.py     # sync + async event streams
python examples/agent_loop_local.py    # iterative refinement + lineage chain
```

`quickstart_local.py` uses `RunBuilder`/`StepBuilder` to construct a Run by hand, then
`Manifest.from_run(run)` and `manifest.verify()`. Output:

```
Run ID:   0618d078-8862-4b6d-b3fb-c725769029e7
Hash:     42c451695e3aa766bf5945dffc7aa384ac4d6649b692c21589c5af101863fae8
Verified: True
```

### With API keys (real generation, real money)

```bash
export GMI_API_KEY="gmi-..." B2_KEY_ID="..." B2_APP_KEY="..."
# or: set -a && source .env && set +a
python examples/quickstart.py
```

### Env var per provider

| Provider | Env var(s) |
|---|---|
| Backblaze B2 (storage) | `B2_KEY_ID`, `B2_APP_KEY` (optional `B2_BUCKET`, `B2_REGION`) |
| GMICloud | `GMI_API_KEY` |
| NVIDIA NIM | `NVIDIA_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Google | `GEMINI_API_KEY` |
| Runway | `RUNWAYML_API_SECRET` |
| Luma | `LUMAAI_API_KEY` |
| Decart | `DECART_API_KEY` |
| Replicate | `REPLICATE_API_TOKEN` |
| ElevenLabs | `ELEVENLABS_API_KEY` |
| Stability AI | `STABILITY_API_KEY` |
| LMNT | `LMNT_API_KEY` |
| Hume | `HUME_API_KEY` |
| AssemblyAI | `ASSEMBLYAI_API_KEY` |

Every key can also be passed explicitly to the constructor
(`GMICloudVideoProvider(api_key=...)`) — the env var is just the default.

### The 30 examples, grouped

- **No keys:** `quickstart_local`, `streaming_local`, `agent_loop_local`
- **Quickstart / storage:** `quickstart`, `b2_storage_pipeline`, `s3_storage_pipeline`
- **Video:** `sora_video_pipeline`, `veo_video_pipeline`, `runway_video_pipeline`,
  `luma_video_pipeline`, `decart_video_pipeline`, `gmicloud_video_pipeline`
- **Image:** `dalle_image_pipeline`, `imagen_pipeline`, `replicate_flux_pipeline`,
  `gmicloud_image_pipeline`
- **Audio:** `tts_audio_pipeline`, `elevenlabs_tts_pipeline`, `elevenlabs_sfx_pipeline`,
  `lmnt_tts_pipeline`, `stability_audio_pipeline`, `gmicloud_audio_pipeline`
- **Advanced:** `chain_image_to_video`, `fan_in_av_composite`, `batch_with_templates`,
  `custom_model_registry`, `error_handling`, `transcribe`, `ingest_podcast_episode`,
  `ingest_ugc_upload`

---

## 12. Where problems actually come from

For a library with no UI, "is anything broken?" is answered by `make test` + `make lint` +
`make typecheck`, plus reading the manifest (it's just JSON) and checking whether the hash
recomputes. **Determinism is the strongest oracle available** — any unexplained hash change
is a real signal.

Failure categories, roughly by likelihood:

1. **Upstream provider drift** — providers rename models, retire slugs, change response
   shapes. Your code doesn't change; it just stops working. `tools/probe_models.py`,
   `tools/probe_gmicloud_wire.py`, and the provider contract tests exist for this. **This
   is the one category that cannot be verified offline** — it needs live API keys.
2. **Schema drift** — a Pydantic model changes without the JSON Schema, and Python/TS
   silently disagree. `test_spec_conformance.py` is the tripwire.
3. **Hash determinism regressions** — anything touching key ordering, float normalization,
   or the exclusion sets silently invalidates every manifest ever written.
4. **Doc drift** — docs carry `last_verified` headers and there's a `/verify-docs` audit,
   because prose rots faster than code.
5. **Silent step failures** — see §5: a `PipelineResult` with `status="failed"` steps and
   no exception raised.

The expected fix loop, per `AGENTS.md`: reproduce as a failing test → fix until it passes →
run the full suite to confirm no invariant broke → update the matching doc **in the same PR**.

---

## 13. Reading order and glossary

### Recommended path

1. `README.md` — product framing, install, the provider matrix
2. `ARCHITECTURE.md` — layout, data flows, ~60 "Core Patterns" bullets (dense but the
   highest-value single file)
3. `AGENTS.md` — conventions and the invariant list
4. `docs/features/pipeline.md` → `provider-system.md` → `manifest-provenance.md` →
   `trust-modes.md`
5. `docs/guides/new-provider.md` — the best way to understand the provider contract
6. `libs/core/genblaze_core/models/manifest.py` — the exclusion sets, ~100 lines,
   the conceptual center
7. `docs/exec-plans/tech-debt-tracker.md` — what the maintainers know is unfinished

### Glossary

| Term | Meaning |
|---|---|
| **Pipeline** | Fluent multi-step workflow; sync, async, and streaming runners |
| **Step** | One generation op — provider, model, prompt, params, retries, fallbacks, cost |
| **Run** | A pipeline execution — steps sharing `run_id`, `tenant_id`, `parent_run_id` |
| **Asset** | A generated artifact — URL, SHA-256, MIME, duration, per-modality metadata |
| **Manifest** | Canonical hash-verified provenance doc; embeddable into media |
| **Provider** | Adapter implementing `submit/poll/fetch_output` |
| **ModelRegistry** | Per-provider store of model specs — pricing, param rules, routing |
| **ModelFamily** | Regex-keyed rule matching a family of model slugs |
| **DiscoverySupport** | `NATIVE`/`PARTIAL`/`NONE` — how well a provider can verify models |
| **Sink** | Output destination — `ObjectStorageSink`, `ParquetSink`, `WebhookSink` |
| **Tracer** | Observability hook — `LoggingTracer`, `OTelTracer`, LangSmith, custom |
| **AgentLoop** | Iterative refinement with parent-linked runs |
| **EmbedPolicy** | Manifest privacy controls — redact prompts, strip params, pointer mode |
| **Canonical hash** | SHA-256 of deterministic JSON with non-deterministic fields excluded |
| **Wave** | A CHANGELOG release grouping; names the git tag, **not** a package version |
| **Preflight** | Pre-run model validation that gates on `ValidationResult` |
| **Trust Mode 1/2/3** | Integrity (ships) / authenticated (roadmap) / C2PA (roadmap) |

### Public API surface

`genblaze_core` exports **110 public names** via lazy imports (`__getattr__` +
`_LAZY_IMPORTS`, with `__dir__` overridden so introspection still works — that was
fix #237). Core ones:

```
Pipeline, PipelineResult, PipelineTemplate, PipelineError, PipelineTimeoutError,
BatchPipelineError, Step, StepBuilder, StepCache, StepTemplate, StepStatus, StepType,
Run, RunBuilder, RunStatus, RunnableConfig, Asset, AssetTransfer, Manifest,
ManifestVerification, ManifestError, UnverifiedAssetError, Modality, PromptVisibility,
PromptTemplate, BaseProvider, SyncProvider, ProviderCapabilities, ProviderError,
ProviderErrorCode, ProviderComplianceTests, ModelRegistry, ModelSpec, ProbeResult,
ProbeStatus, ObjectStorageSink, ParquetSink, WebhookSink, WebhookNotifier, BaseSink,
KeyStrategy, KeyBuilder, StorageBackend, StorageConfig, StorageError, URLPolicy,
Tracer, NoOpTracer, LoggingTracer, OTelTracer, CompositeTracer, StructuredLogger,
AgentLoop, AgentContext, AgentIteration, AgentResult, Evaluator, CallableEvaluator,
ThresholdEvaluator, EvaluationResult, EmbedPolicy, EmbeddingError, ModerationHook,
ModerationResult, FFmpegCompositor, FFmpegTransform, MockProvider, MockVideoProvider,
MockAudioProvider, ChatMessage, ChatResponse, ToolCall, TextContent, Voice, WordTiming,
VideoMetadata, AudioMetadata, Track, StreamEvent, ProgressEvent, StepCompleteEvent,
GenblazeError, SinkError, parse_manifest, discover_providers, validate_asset_url,
validate_chain_input_url
```

Install names use hyphens, imports use underscores: `pip install genblaze-openai` →
`import genblaze_openai`.

TypeScript consumers: `npm install @genblaze/spec` for generated `.d.ts` manifest types.

---

## 14. What this brief does *not* cover

Stated plainly so the reading AI doesn't fill gaps with invention:

- **No live provider call was ever made.** Everything verified here is offline. Whether a
  real Sora or Veo response matches what the adapter expects is untested in this
  investigation, and it's the single largest unverified area.
- **Only 3 of 18 packages were installed and exercised** (`libs/core`, `cli`,
  `libs/connectors/s3`). The full `make test` across all 15 connectors was **not** run.
- **Connector internals were surveyed, not read.** Class names and capability matrices are
  accurate; per-provider param quirks, error mappings, and retry specifics are not covered.
- **Untouched areas:** `ParquetSink` and `genblaze index` in practice; `WebhookSink`
  delivery; `PipelineTemplate` round-trips; `StepCache` behavior; the `ingest`/`import`
  non-generative step types; `libs/spec` TS codegen; the batch-issue orchestration tooling
  in `tools/`; the maintainer-agent workflow.
- **CHANGELOG.md (188 KB) was not read** — it holds the real per-release history and would
  add a lot of context this brief lacks.
- **Version-pinned facts.** Everything reflects commit `a60056b`. The `raise_on_failure`
  default is scheduled to flip in 0.4.0 (the umbrella is already at 0.4.5, so parts of this
  may already be stale for newer core releases).
