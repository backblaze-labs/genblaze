<!-- last_verified: 2026-04-17 -->
# Architecture

## Components

- **genblaze-core** (`libs/core/`) — Python SDK: Pydantic v2 models, builders, canonical JSON, media handlers, sinks, pipeline, agents, observability
- **Provider adapters** (`libs/connectors/`) — One package per provider:
  - `genblaze-openai` — OpenAI (Sora video; DALL-E + gpt-image family image generation & edits; TTS audio)
  - `genblaze-google` — Google GenAI (Veo video, Imagen image)
  - `genblaze-runway` — Runway (Gen-4 Turbo video)
  - `genblaze-luma` — Luma (Dream Machine video)
  - `genblaze-decart` — Decart (Lucy video/image)
  - `genblaze-replicate` — Replicate (multi-model hub)
  - `genblaze-elevenlabs` — ElevenLabs (TTS + sound effects)
  - `genblaze-stability-audio` — Stability AI (Stable Audio music)
  - `genblaze-lmnt` — LMNT (fast TTS)
  - `genblaze-gmicloud` — GMICloud (video, image, audio via request queue)
- **genblaze-s3** (`libs/connectors/s3/`) — S3-compatible storage backend
- **genblaze-langsmith** (`libs/connectors/langsmith/`) — LangSmith observability tracer
- **genblaze-cli** (`cli/`) — Click-based CLI: extract, verify, replay, index
- **JSON Schemas** (`libs/spec/schemas/manifest/v1/`) — Language-neutral schema definitions

## Deployment

- All packages are installable via pip (`genblaze-core`, `genblaze-replicate`, `genblaze-cli`)
- Library-only — no running services; users embed into their own applications
- `pyarrow` is an optional dependency via `genblaze-core[parquet]`

## Data Model Hierarchy

- **Manifest** → contains a **Run** → contains **Steps** → contain **Assets**
- All IDs are UUIDs
- Manifests include a `canonical_hash` (SHA-256 of deterministic JSON)
- Assets carry optional typed metadata: `VideoMetadata` (codec, frame_rate, resolution, has_audio), `AudioMetadata` (sample_rate, channels, codec, word_timings), and `Track` list (kind, codec, label) for multi-stream containers

## Data Stores

- **Object storage** — S3-compatible upload via `ObjectStorageSink`. Backblaze B2 is the recommended default backend (`S3StorageBackend.for_backblaze(...)`); AWS S3, Cloudflare R2, and MinIO are supported via the generic constructor.
  - **HIERARCHICAL** (run-grouped):
    ```
    {prefix}/runs/{tenant}/{date}/{run_id}/manifest.json
    {prefix}/runs/{tenant}/{date}/{run_id}/assets/{asset_id}.ext
    ```
  - **CONTENT_ADDRESSABLE** (deduped):
    ```
    {prefix}/assets/{sha256[:2]}/{sha256[2:4]}/{sha256}.ext
    {prefix}/manifests/{run_id}.json
    ```
  - Canonical files: `libs/core/genblaze_core/storage/sink.py`, `transfer.py`, `base.py`
- **Parquet sink** — Partitioned by `dt=/tenant_id=/modality=/provider=`
  - Three tables: `runs/`, `steps/`, `assets/`
  - Idempotent writes keyed by `run_id`
- **Media embedding** — Manifests embedded inline (PNG iTXt, JPEG/WebP XMP, MP4 UUID box, MP3 ID3v2 TXXX, WAV LIST/INFO) or as sidecar JSON

## External Services

- **OpenAI API** — Sora video; DALL-E + gpt-image family (`gpt-image-2`, `gpt-image-1.5`, `gpt-image-1`, `gpt-image-1-mini`) image generation and edits; TTS audio (`genblaze-openai`)
- **Google GenAI API** — Veo video, Imagen image (`genblaze-google`)
- **Runway API** — Gen-4 Turbo video (`genblaze-runway`)
- **Luma API** — Dream Machine video (`genblaze-luma`)
- **Decart API** — Lucy video/image (`genblaze-decart`)
- **Replicate API** — Multi-model hub (`genblaze-replicate`)
- **ElevenLabs API** — TTS + sound effects (`genblaze-elevenlabs`)
- **Stability AI API** — Stable Audio music (`genblaze-stability-audio`)
- **LMNT API** — Fast TTS (`genblaze-lmnt`)
- **GMICloud API** — Video, image, audio via request queue (`genblaze-gmicloud`)
- All accessed via lazy SDK imports — no runtime dependency unless the connector is used

## Trust Boundaries

- Provider adapters handle API tokens — never stored in manifests
- `EmbedPolicy` controls what data gets embedded (prompt redaction, pointer mode)
- Canonical JSON ensures hash integrity across serialize/deserialize

## Data Flows

- **Generation**: Pipeline → StepCache check → Provider (submit/poll/fetch_output) → Step with Assets → Run → Manifest
- **Embedding**: Manifest → EmbedPolicy filter → SmartEmbedder → media file (inline or sidecar)
- **Extraction**: media file → Handler.extract() → Manifest → verify() against canonical_hash
- **Sink**: Run + Manifest → ParquetSink → partitioned Parquet files

## Core Patterns

- `Runnable[In, Out]` ABC with `invoke`/`ainvoke`
- Providers implement 3-method lifecycle: `submit/poll/fetch_output`
- Fluent builders: `StepBuilder`, `RunBuilder`; manifests via `Manifest.from_run()`
- Canonical JSON: deterministic key sorting + float normalization + Unicode NFC + SHA-256
- Pipeline: `batch_run`/`abatch_run` for multi-prompt execution with semaphore-based concurrency control
- Pipeline concurrency: `arun()` with `chain=False` runs steps concurrently; `max_concurrency` limits parallelism
- Pipeline fan-in: `input_from` on `.step()` routes outputs from specific prior steps (by index) into a later step, enabling AV mux patterns
- Pipeline-level timeout: `pipeline_timeout` raises `PipelineTimeoutError` when wall-clock time exceeds limit
- `on_submit` callback: fires after `submit()` with `(step_id, prediction_id)` for crash-recovery checkpointing
- Parameter normalization: `provider.normalize_params()` maps standard names (duration, resolution) to native ones
- Model fallback chains: `fallback_models` in `.step()` auto-retries with alternate models on `MODEL_ERROR`
- Cost tracking: providers populate `step.cost_usd` from static pricing tables
- Capability validation: `ProviderCapabilities.accepts_chain_input` flag; pipeline validates modality + chain compatibility at `run()` time before executing any steps
- Chain input validation: `validate_chain_input_url()` checks chain input URLs before forwarding to external APIs (allows `file://` + `https://`)
- Pipeline chain safety: failed steps in `chain=True` mode clear `prev_assets` so subsequent steps receive empty inputs (no stale output leakage)
- `PipelineResult.error_summary()`: aggregates step errors and transfer failures into a single string
- Adaptive polling: poll intervals increase over time; `SubmitResult` enables provider timing hints
- Streaming transfer: `AssetTransfer` streams large files to disk via `SpooledTemporaryFile` instead of RAM
- Parallel asset upload: `ObjectStorageSink` uploads assets concurrently via `ThreadPoolExecutor` (configurable `max_upload_workers`)
- Large MP4 support: MP4 handler uses seek-based I/O for files 500 MB–2 GB (in-memory for smaller files)
- FFmpeg compositing: `FFmpegCompositor` SyncProvider muxes video + audio into MP4 via ffmpeg subprocess
- FFmpeg transforms: `FFmpegTransform` SyncProvider for resize, crop, overlay_text, audio_normalize, and format conversion
- Prompt templates: `PromptTemplate` with `{variable}` placeholders for batch workflows; `batch_run` accepts `list[dict]`
- Pipeline templates: `PipelineTemplate` serializable pipeline definitions (JSON); `Pipeline.to_template()` for export
- Moderation hooks: `ModerationHook` ABC with `check_prompt`/`check_output` pre/post-step content screening
- Webhook notifications: `WebhookNotifier` fire-and-forget HTTP status events via background thread; HTTPS-only URLs validated at construction, DNS-resolved against private IP ranges on first dispatch
- SSRF protection: shared `check_ssrf()` in `_utils.py` blocks private/loopback IPs; used by both `AssetTransfer` and `WebhookNotifier`
- OTel bridging: `StepSpan` optionally starts real OpenTelemetry spans when the SDK is installed
- Tracer abstraction: pluggable `Tracer` ABC with NoOp/Logging/OTel/Composite backends; routes run+step lifecycle hooks + StreamEvents
- Streaming: `Pipeline.stream()` / `astream()` yield `StreamEvent` iterators; events also forwarded to the attached tracer
- Agent loop: `AgentLoop` composes a `Pipeline` factory with an `Evaluator`; each iteration linked via `parent_run_id` for lineage-preserving retry

## Canonical Files

- Runnable ABC: `libs/core/genblaze_core/runnable/base.py`
- Provider interface: `libs/core/genblaze_core/providers/base.py`
- Replicate adapter: `libs/connectors/replicate/genblaze_replicate/provider.py`
- Pipeline: `libs/core/genblaze_core/pipeline/pipeline.py`
- Step cache: `libs/core/genblaze_core/pipeline/cache.py`
- Canonical JSON: `libs/core/genblaze_core/canonical/json.py`
- Media handler base: `libs/core/genblaze_core/media/base.py`
- SmartEmbedder: `libs/core/genblaze_core/media/embedder.py`
- Parquet sink: `libs/core/genblaze_core/sinks/parquet.py`
- MP4 handler: `libs/core/genblaze_core/media/mp4.py`
- FFmpegCompositor: `libs/core/genblaze_core/providers/compositor.py`
- FFmpegTransform: `libs/core/genblaze_core/providers/transform.py`
- FFmpeg utilities: `libs/core/genblaze_core/providers/_ffmpeg_utils.py`
- PromptTemplate: `libs/core/genblaze_core/models/prompt_template.py`
- PipelineTemplate: `libs/core/genblaze_core/pipeline/template.py`
- ModerationHook: `libs/core/genblaze_core/pipeline/moderation.py`
- Webhook notifier: `libs/core/genblaze_core/webhooks/notifier.py`
- Webhook sink: `libs/core/genblaze_core/webhooks/sink.py`
- MP3 handler: `libs/core/genblaze_core/media/mp3.py`
- WAV handler: `libs/core/genblaze_core/media/wav.py`
- EmbedPolicy: `libs/core/genblaze_core/models/policy.py`
- Data models: `libs/core/genblaze_core/models/`
- StreamEvent: `libs/core/genblaze_core/observability/events.py`
- Tracer ABC + backends: `libs/core/genblaze_core/observability/tracer.py`
- Streaming helpers: `libs/core/genblaze_core/pipeline/streaming.py`
- Agent loop: `libs/core/genblaze_core/agents/loop.py`
- Evaluator: `libs/core/genblaze_core/agents/evaluator.py`
- LangSmith tracer: `libs/connectors/langsmith/genblaze_langsmith/tracer.py`

## Features

- [Pipeline](docs/features/pipeline.md)
- [Streaming](docs/features/streaming.md)
- [Observability](docs/features/observability.md)
- [Agents](docs/features/agents.md)
- [Prompt Templates](docs/features/prompt-templates.md)
- [Asset Transforms](docs/features/asset-transforms.md)
- [Pipeline Templates](docs/features/pipeline-templates.md)
- [Moderation](docs/features/moderation.md)
- [Webhooks](docs/features/webhooks.md)
- [Manifest Provenance](docs/features/manifest-provenance.md)
- [Media Embedding](docs/features/media-embedding.md)
- [Provider System](docs/features/provider-system.md)
- [Model Registry](docs/features/model-registry.md)
- [Embed Policy](docs/features/embed-policy.md)
- [Iteration & Lineage](docs/features/iteration.md)
- [Object Storage](docs/features/object-storage.md)
- [Parquet Sink](docs/features/parquet-sink.md)
- [Queue Integration](docs/features/queue-integration.md)
- [CLI](docs/features/cli.md)
