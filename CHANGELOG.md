# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.6] - 2026-04-24

### Released package versions
- Code-change bumps: `genblaze-core` 0.2.5, `genblaze-gmicloud` 0.2.5.
- Untouched (no republish): every other Python and npm package.

### Added
- `genblaze-core`: `providers/probe.py` — model-probe contract for runtime
  capability discovery against a live provider, plus a conformance test
  suite (`tests/conformance/test_provider_contract.py`) that every provider
  adapter must pass.
- `genblaze-core`: `providers/params.py` — shared parameter-standardization
  hooks (canonical → native rewriting at the provider boundary).
- `genblaze-core`: `models/voice.py` — first-class `Voice` model for TTS
  providers.
- `genblaze-core`: `Pipeline` gains `batch_items` / `batch_raise` /
  `estimated_cost` / `raise_on_failure` — execution controls covered by
  four new dedicated test files.
- `genblaze-core`: `exceptions.py` expanded with new typed errors aligned to
  the probe + standardization contracts.
- `genblaze-gmicloud`: model registries fully reconciled with live GMICloud
  catalog — image/audio/video spec rewrites, new `models/voices.py` (172
  lines) for the audio voice catalog, standardization-hook tests.
- Tooling: `tools/probe_models.py` and `tools/gen_model_matrix.py` for
  generating the model-status matrix from live API probes (not part of any
  published package; repo-only utilities).
- Docs: `docs/reference/model-matrix.md`, `docs/reference/model-probe-status.json`.

## [0.2.5.post1] - 2026-04-24

### Fixed
- `genblaze` 0.3.1: corrects the `genblaze-nvidia` pin in the `nvidia` extra
  and the `video`/`image`/`audio`/`all` bundles. 0.3.0 pinned
  `genblaze-nvidia>=0.1.0,<0.2` but nvidia actually shipped at 0.2.0, which
  made `pip install "genblaze[nvidia]"` unresolvable. Widened to
  `>=0.2.0,<0.3`.

## [0.2.5] - 2026-04-24

### Released package versions
- **New on PyPI (first publish):** `genblaze-nvidia` 0.2.0 (NIM /
  build.nvidia.com adapters for video, image, audio, chat — aligned with
  release train), `genblaze-cli` 0.2.0 (`genblaze` CLI script for
  `extract` / `verify` / `replay` / `index`).
- **New minor on PyPI:** `genblaze` (umbrella) 0.3.0 — now ships real code
  that lazily re-exports `genblaze_core`'s public API, so
  `from genblaze import Pipeline` works. `genblaze_core` stays the canonical
  import path in docs. Also exposes the `nvidia` extra and adds NVIDIA to the
  `video` / `image` / `audio` / `all` curated bundles.
- Code-change bumps from retry-policy unification: `genblaze-core` 0.2.4,
  `genblaze-gmicloud` 0.2.4, `genblaze-google` 0.2.3, `genblaze-openai` 0.2.3,
  `genblaze-replicate` 0.2.2, `genblaze-runway` 0.2.2, `genblaze-decart` 0.2.2,
  `genblaze-elevenlabs` 0.2.2, `genblaze-lmnt` 0.2.2, `genblaze-luma` 0.2.2,
  `genblaze-stability-audio` 0.2.2.
- `@genblaze/spec` (npm) 0.3.2 — adds `events/v1/step-retried.schema.json`
  and updates the `stream-event` union for the new event.
- Untouched (no republish): `genblaze-s3` 0.2.3, `genblaze-langsmith` 0.2.1.

### Added
- `genblaze-core`: `genblaze_core.providers.retry` — unified retry policy
  module. Every provider adapter now delegates transient-error classification
  + exponential backoff to this shared surface instead of rolling its own.
  Consistent behavior across GMICloud, OpenAI, Google, Replicate, Runway,
  Luma, Decart, ElevenLabs, LMNT, Stability Audio, NVIDIA. Exposes a
  `RetryPolicy` the caller can override per-provider.
- `genblaze-core`: `BaseProvider` retry plumbing baked into `invoke` /
  `ainvoke` / `resume` / `aresume`. Also surfaces retry attempts as new
  `StepRetried` stream events for observability.
- `genblaze-nvidia` (new package): video (Cosmos), image (Flux, SDXL, etc.),
  audio (TTS, Parakeet ASR), and chat (OpenAI-compatible NIM endpoints)
  providers for NVIDIA's NIM / build.nvidia.com platform. Ships with 1146
  lines of tests.
- `genblaze-cli` (first public publish): the `genblaze` command-line tool
  with `extract`, `verify`, `replay`, and `index` subcommands for manifest
  operations. Script entry point: `genblaze`.
- `genblaze` (umbrella) 0.3.0: now ships a real Python package (was an empty
  metapackage). Re-exports the top-level public surface of `genblaze_core`
  lazily, so `from genblaze import Pipeline` works after
  `pip install genblaze`. `genblaze_core` remains the canonical import path
  used in docs and examples. Submodules (`genblaze_core.media`,
  `genblaze_core.canonical`) and provider adapters (`genblaze_openai`,
  `genblaze_google`, …) are not re-exported — import those from their own
  packages. Ships `py.typed` for static type-checker support.
- `@genblaze/spec`: new `events/v1/step-retried.schema.json` schema + TS type
  for the retry event.

## [0.2.4] - 2026-04-24

### Released package versions
- Code-change bumps: `genblaze-core` 0.2.3, `genblaze-gmicloud` 0.2.3,
  `genblaze-google` 0.2.2, `genblaze-openai` 0.2.2.
- `@genblaze/spec` (npm) 0.3.1 — minor schema + TS-type touch-up.
- Untouched since last wave (no republish): `genblaze-s3` 0.2.3,
  `genblaze-replicate` 0.2.1, `genblaze-decart` 0.2.1, `genblaze-elevenlabs`
  0.2.1, `genblaze-langsmith` 0.2.1, `genblaze-lmnt` 0.2.1, `genblaze-luma`
  0.2.1, `genblaze-runway` 0.2.1, `genblaze-stability-audio` 0.2.1,
  `genblaze` (umbrella) 0.2.3 — its dep ranges already satisfy the new
  versions.

### Added
- `genblaze-gmicloud`: HTTP client injection in `GMICloudBase` — providers
  accept an optional `client=` for deterministic tests and custom transport
  (proxies, retries, observability). New `test_base_client_injection.py`
  covers the contract. Part of the gmi-hardening exec plan.
- `genblaze-google`: `_errors.py` module with explicit `ProviderErrorCode`
  mapping for Gemini SDK exceptions. New `test_error_mapping.py`.
- `genblaze-openai`: `test_chat.py` covers the `chat()` / `achat()` wrapper
  paths end-to-end.
- `genblaze-core`: new `Modality` enum member.

### Fixed
- `genblaze-core`: OTel tracer event-type check no longer misclassifies a
  subset of events; poll-cache cleanup test hardened.
- `genblaze-gmicloud`: media URL extraction handles a previously-missed
  envelope shape; chat client initialization condition corrected.

### Added
- `genblaze-core`: `ChatMessage`, `ToolCall`, `ChatResponse` in
  `genblaze_core.models.chat` — uniform return shape for the standalone
  chat wrappers below. Not part of the manifest wire protocol.
- `genblaze-openai`, `genblaze-google`, `genblaze-gmicloud`: standalone
  `chat()` / `achat()` wrappers around each provider's chat / completion
  endpoint. Sit outside the Pipeline / Step machinery — convenience for
  callers driving media steps from an LLM. See
  `docs/features/llm-calls.md`.
- `genblaze-core`: `ProviderErrorCode.CONTENT_POLICY` — new normalized
  code for safety / content-policy refusals. Deterministic, never
  retryable. Wire schema (`step.schema.json`) and TS types regenerated.
  Shared `classify_api_error()` detects the common keywords; per-provider
  mappers (Google, GMICloud) prioritize policy detection over status-code
  classification so a 400 policy refusal isn't misclassified as
  `INVALID_INPUT`.
- `genblaze-gmicloud`: `base_url=` ctor kwarg (+ `GMI_BASE_URL` env) and
  `http_client=` kwarg on all three provider classes. Enables staging /
  proxy / VPC deployments and lets multi-modality pipelines share one
  `httpx.Client` across video / image / audio providers. Externally
  supplied clients are never closed by `close()`.
- Root README: provider × modality capability matrix now includes a
  "Chat (LLM)" column — single-page answer to "which connector does
  what?".

### Fixed
- `genblaze-gmicloud`: `GMICloudImageProvider.fetch_output` now emits one
  `Asset` per URL in the `media_urls` envelope. Previously discarded all
  but the first when `number_of_images > 1`, silently returning one asset
  for an N-asset bill. The new `extract_media_urls()` helper exposes the
  full list; `extract_media_url()` remains as a single-output thin
  wrapper for the video and audio paths.

### Changed
- `genblaze-core`: `ModelRegistry` now logs dropped non-allowlisted
  params at `INFO` (was `DEBUG`). Silent allowlist drops now surface in
  typical production logs without WARNING-level noise.
- `genblaze-gmicloud` README: removed the SDK email/password auth claim
  (only API-key auth is implemented). Added a naming-reference table,
  the `base_url` / `http_client` injection pattern, LLM-access surface,
  and the canonical status-check idiom for reading `step.assets` safely.
- `GMICloudAudioProvider` docstring now documents that audio input is a
  reference voice for cloning, not a source for speech-to-text. STT is
  out of scope for this class.

## [0.2.3] - 2026-04-23

### Released package versions
- **New:** `genblaze` 0.2.3 — umbrella metapackage. `pip install genblaze`
  installs `genblaze-core` + `genblaze-s3` by default; provider adapters are
  opt-in extras (e.g. `pip install "genblaze[gmicloud,video]"`). Curated
  bundles: `[video]`, `[image]`, `[audio]`, `[all]`.
- Code-change bumps: `genblaze-core` 0.2.2, `genblaze-replicate` 0.2.1,
  `genblaze-s3` 0.2.3.
- Metadata-only force-bumps (author + Homepage fill-in, no code changes):
  `genblaze-gmicloud` 0.2.2, `genblaze-openai` 0.2.1, `genblaze-google` 0.2.1,
  `genblaze-decart` 0.2.1, `genblaze-elevenlabs` 0.2.1, `genblaze-langsmith`
  0.2.1, `genblaze-lmnt` 0.2.1, `genblaze-luma` 0.2.1, `genblaze-runway` 0.2.1,
  `genblaze-stability-audio` 0.2.1.
- `@genblaze/spec` (npm) 0.3.0 — minor bump for new events schema namespace.

### Added
- `genblaze` metapackage for discoverable `pip install genblaze` UX.
- Every published Python package now has `authors` and `Homepage` URL
  populated, so `pip show` and the PyPI project page render correctly.
- Root README: "Install" section with package-to-import mapping table
  (resolves hyphen/underscore confusion for new users).

### Added
- `genblaze-core`: `genblaze_core.pipeline` package now uses PEP 562
  module-level `__getattr__` for lazy attribute resolution (`Pipeline`,
  `StepCache`, `PipelineResult`, `StepCompleteEvent`). `from
  genblaze_core.pipeline import Pipeline` still works; loading the heavy
  `pipeline.py` module is deferred until first access. Lets
  `observability.events` import from `pipeline.result` without a
  circular import through `pipeline.py`.
- `libs/spec/ts/genblaze.d.ts` — TypeScript type declarations generated
  from the JSON Schemas. Eliminates hand-rolled type drift in downstream
  TS consumers (studio UIs, Node backends). Regenerate via
  `make ts-types`. Phase 1a: committed in-repo; phase 1b will publish
  `@genblaze/spec` to npm. See `libs/spec/README.md`.
- `libs/core/tests/unit/test_spec_conformance.py` — bidirectional
  conformance tests between Pydantic models and `libs/spec/schemas/`.
  Catches field-set drift, enum drift, missing descriptions. Runs under
  `make test`.
- CI `ts-types` job — drift guard that regenerates the TS types and
  fails if the committed file would change.
- `genblaze-core`: `StreamEvent` is now a Pydantic discriminated union —
  ten per-variant classes (`PipelineStartedEvent`, `PipelineCompletedEvent`,
  `PipelineFailedEvent`, `StepStartedEvent`, `StepProgressEvent`,
  `StepCompletedEvent`, `StepFailedEvent`, `AgentIterationStartedEvent`,
  `AgentIterationEvaluatedEvent`, `AgentCompletedEvent`) under a common
  `StreamEvent` base. `AnyStreamEvent` + `StreamEventAdapter` (a
  `TypeAdapter`) parse inbound event dicts into the correct variant via
  the `type` discriminator.
- `libs/spec/schemas/events/v1/*.schema.json` — Draft 2020-12 JSON Schemas
  for all ten variants plus a parent `stream-event.schema.json` with
  `oneOf` + `discriminator`. Generated TypeScript types now include the
  full discriminated-union `StreamEvent` surface.
- Conformance coverage extended to event variants (field-set parity,
  description required, `additionalProperties: false`, `type` const
  matches Pydantic `Literal`, round-trip validation, discriminator
  completeness).

### Changed
- `libs/spec/schemas/manifest/v1/policy.schema.json`:
  `prompt_visibility` enum now includes `"encrypted"` to match the
  Pydantic `PromptVisibility` enum. Schema-only fix; Pydantic
  acceptance was already unchanged.
- `Asset.url` field description now documents the durable-URL-is-the-
  handle invariant (no separate storage-key field; parse key from URL
  if needed) so the contract flows into JSON Schema and generated
  TypeScript JSDoc.
- **Breaking (pre-1.0)**: `StreamEvent(type=..., ...)` with a raw
  discriminator string no longer validates — construct the specific
  variant class (`StepStartedEvent(...)`, etc.). `isinstance(ev,
  StreamEvent)` still narrows all variants; per-variant narrowing via
  `isinstance(ev, StepFailedEvent)` or `ev.type == "step.failed"` is now
  supported and produces precise field types under pyright/mypy. Agent
  events flatten their former `data` dict into proper fields — e.g.
  `event.data["iteration"]` is now `event.iteration`.
- **Breaking wire format for `step.failed`**: the serialized event no
  longer carries a `message` key — the failure reason lives on a
  dedicated `error` field. Previously the failure string was duplicated
  into both `message` and `error`. Webhook / log / SSE consumers that
  key on `message` for failed steps should switch to `error`.
- `StreamEvent.to_dict()` now delegates to `model_dump(mode="json",
  exclude_none=True)` and always emits `type`+`timestamp` plus the
  variant's declared fields. In-process-only fields (`step`, `result`)
  remain on the Python object but are excluded from the wire shape;
  derived `step_status`/`manifest_hash`/`run_status`/`error` fields are
  pre-populated at construction so consumers don't lose context.
- `StepCompletedEvent.step` / `StepFailedEvent.step` are now typed as
  `Step | None` (was `Any`); `PipelineCompletedEvent.result`,
  `PipelineFailedEvent.result`, `AgentIterationEvaluatedEvent.result`,
  and `AgentCompletedEvent.result` are typed as `PipelineResult | None`.
  Pydantic passes instances through by identity
  (`revalidate_instances="never"`) — no copy, no perf regression. IDE
  autocomplete and static type checkers now narrow `event.step.assets`
  and `event.result.manifest` correctly.

## [0.2.2] - 2026-04-23

### Released package versions
- `genblaze-core` 0.2.1, `genblaze-gmicloud` 0.2.1, `genblaze-s3` 0.2.2.
- First-time PyPI releases at 0.2.0: `genblaze-decart`, `genblaze-elevenlabs`,
  `genblaze-langsmith`, `genblaze-lmnt`, `genblaze-luma`, `genblaze-replicate`,
  `genblaze-runway`, `genblaze-stability-audio`. All pin
  `genblaze-core>=0.2.0,<0.3`.
- `genblaze-openai` and `genblaze-google` remain at 0.2.0 — no code changes
  since the 0.2.0 release.

### Added
- `genblaze-core`: `ModelSpec.deprecated_aliases` — old model ids keep resolving
  but emit a `DeprecationWarning` pointing to the canonical slug. Drop the
  alias after one minor version.
- `genblaze-core`: `ModelRegistry.resolve_canonical(model_id)` — returns the
  canonical slug the upstream API expects (or passes caller input through when
  only the fallback spec matches). Use in connectors with case-sensitive
  upstream APIs instead of poking at `spec.model_id`.
- `genblaze-gmicloud`: `extract_media_url()` envelope parser covering both the
  live `outcome.media_urls[0].url` shape and the legacy flat `*_url` keys.
  Image modality also falls back to `outcome.thumbnail_image_url`.
- `genblaze-gmicloud`: legacy-slug and error-unwrap test coverage.

### Changed
- `genblaze-gmicloud`: all image and video model ids rewritten to the live
  lowercase slugs the request-queue API actually accepts (`seedream-5.0-lite`,
  `veo3`, `wan2.6-i2v`, `kling-text2video-v1.6-pro`, etc.). Old PascalCase ids
  (`Seedream-5.0-Lite`, `Veo3`, `Wan-2.6-I2V`, …) still resolve via
  `deprecated_aliases` and will be removed in 0.4.
- `genblaze-gmicloud`: submits now send the canonical slug on the wire, not the
  caller-supplied string — matters because the GMICloud request queue is
  case-sensitive.
- `genblaze-gmicloud`: JSON error bodies (`{"error": "..."}`) are unwrapped
  before being surfaced, replacing the confusing double-encoded
  `GMICloud submit failed (500): {"error":"Backend error (400)..."}` message.
- `genblaze-gmicloud`: new model families registered — reve-create/edit/remix
  (+ fast variants), bria-fibo-* (blend/relight/restore/genfill/eraser),
  pixverse-v5.6 i2v/t2v/transition, wan2.6/2.7 t2v/i2v/r2v.

### Fixed
- `genblaze-gmicloud`: `fetch_output` parsed the wrong envelope path; live API
  returns `outcome.media_urls[0].url` but the connector read `outcome.*_url`
  keys only. Mock fixtures were aligned with the real shape in the same change.

## [0.2.1] - 2026-04-23

### Changed
- `genblaze-s3`: dependency pin widened to `genblaze-core>=0.1.0,<0.3` so the
  S3 backend is installable alongside core 0.2.x (ModelRegistry release).
  No code changes; s3 is compatible with both 0.1.x and 0.2.x core.

## [0.2.0] - 2026-04-23

### Added
- `genblaze-core`: `ModelRegistry` + `ModelSpec` — declarative, user-extensible
  per-model configuration across every provider connector. New public surface
  at `genblaze_core.providers`: `ModelSpec`, `ModelRegistry`, `PricingContext`,
  `PricingStrategy`, pricing helpers (`per_unit`, `per_input_chars`,
  `per_output_second`, `per_response_metric`, `tiered`, `bucketed_by_duration`,
  `by_param`, `by_model_and_param`, `first_match`), input routers
  (`route_images`, `route_audio`, `route_by_media_type`, `route_keyframes`,
  `chain_routers`), constraints (`requires_together`, `mutually_exclusive`,
  `required_one_of`, `implies`), and param schemas (`IntSchema`, `EnumSchema`,
  `StringSchema`, `BoolSchema`, `FloatSchema`, `ArraySchema`).
- `genblaze-core`: `BaseProvider.create_registry()`, `models_default()`,
  `prepare_payload(step)`, and `models=` ctor kwarg. Registry pricing is
  applied automatically after `fetch_output()` when `step.cost_usd` is unset.
- All 12 provider connectors (`gmicloud` video/image/audio, `openai`
  dalle/tts/sora, `google` imagen/veo, `elevenlabs` tts/sfx, `replicate`,
  `runway`, `luma`, `stability-audio`, `lmnt`, `decart` image/video) migrated
  to `ModelRegistry`. Inline `_PRICING` / `_MODELS` / `forward_keys` dicts
  removed; data now lives in spec-building functions.
- Docs: `docs/features/model-registry.md`, README quickstart for custom
  pricing/models, runnable `examples/custom_model_registry.py` (no API keys
  needed).

### Fixed
- `genblaze-s3`: credentials preserved across region auto-detection. Previously
  `_reconfigure_for_region` tried to recover credentials from
  `boto3.client.meta.config.__dict__`, which doesn't hold them — the
  reconfigured client silently lost creds and failed mid-upload with
  `NoCredentialsError`. Credentials are now persisted on the backend at
  construction and threaded through the rebuild.
- `genblaze-s3`: region preflight runs on `get`/`exists`/`delete`/`get_url`
  (presigned), not just `put`. Previously the first `exists()` call —
  routinely made by `ObjectStorageSink` before `put` — could skip
  verification and hit the wrong region.
- `genblaze-s3`: region-redirect endpoint rewrite now only fires for B2
  endpoints. AWS S3 / R2 / MinIO users hitting a 301 are no longer
  silently retargeted at `s3.{region}.backblazeb2.com`.
- `genblaze-s3`: explicit `ChecksumSHA256` in `extra_args` now routes
  through `put_object` (single-PUT). Whole-object SHA-256 is only valid
  for single-part uploads; the previous path would have let
  `upload_fileobj` take the multipart code path with an invalid header.
- `genblaze-core`: `ObjectLockConfig` rejects naive datetimes with a
  clear error. S3's handling of naive timestamps is ambiguous and we
  refuse to silently accept multi-year retention with a wrong anchor.
  Past retention still allowed but logs a loud warning.
- Regression tests added for every item above.

### Added
- `genblaze-core`: `ObjectLockConfig` dataclass + `ObjectStorageSink(manifest_lock=...)`
  parameter. Applies B2 Object Lock retention (GOVERNANCE or COMPLIANCE) to
  manifest uploads — turns genblaze's canonical-hash provenance into an
  immutable, audit-grade on-disk artifact. GOVERNANCE is the default;
  COMPLIANCE logs a prominent warning at construction because its retention
  cannot be shortened, even by the account root. See
  `docs/features/object-storage.md` for the full recipe.
- `genblaze-s3`: multipart uploads via `upload_fileobj` + `TransferConfig` —
  assets >16 MB now split into 16 MB parts uploaded 4-way in parallel, each
  part individually retryable. Transforms multi-GB video uploads from a
  lottery ticket into a reliable operation.
- `genblaze-s3`: per-part SHA-256 integrity via `ChecksumAlgorithm=SHA256`
  on every upload so B2 server-side-verifies transfer integrity.
- `genblaze-s3`: `S3StorageBackend.ensure_lifecycle_defaults()` helper that
  applies idempotent `AbortIncompleteMultipartUpload` (7 days) and
  `NoncurrentVersionExpiration` (30 days) rules. Called automatically by
  `for_backblaze(auto_lifecycle=True)` (default).
- `genblaze-s3`: automatic bucket-region auto-detection — the first
  `put()`/`exists()` call runs a HeadBucket preflight and transparently
  reconfigures the client if the bucket lives in a different region.
- `genblaze-s3`: `StorageBackend.put()` gains an `extra_args` passthrough
  for boto3-style `ExtraArgs` (Cache-Control, SSE, Object Lock, etc.).
- `genblaze-core`: immutable Cache-Control on CONTENT_ADDRESSABLE uploads
  (`public, max-age=31536000, immutable`), shorter private TTL on
  HIERARCHICAL. Unlocks the B2 + Cloudflare Bandwidth Alliance zero-egress
  delivery pattern documented in `docs/features/object-storage.md`.
- Docs: "Serving media at zero egress: B2 + Cloudflare" recipe.

### Changed
- `genblaze-s3`: `for_backblaze()` raises a clear `ValueError` when both
  `B2_KEY_ID`/`B2_APP_KEY` env vars and explicit `key_id`/`app_key` are
  missing — prevents opaque mid-upload `NoCredentialsError`.
- `genblaze-s3`: `BotoConfig` now pins
  `request_checksum_calculation="when_required"` /
  `response_checksum_validation="when_required"` so boto3 never sends
  `x-amz-sdk-checksum-algorithm` trailer headers that older B2 deployments
  and other S3-compat endpoints reject. Genblaze sets SHA-256 explicitly.
- `genblaze-s3`: default `max_pool_connections` bumped to 20 to accommodate
  concurrent multipart uploads.

## [0.1.0] - 2026-04-22

### Added
- Core models: `Asset`, `Step`, `Run`, `Manifest`
- Fluent builders: `StepBuilder`, `RunBuilder`, `ManifestBuilder`
- Canonical JSON serialization with SHA-256 hashing
- Unicode NFC normalization in canonical JSON
- New enums: `RunStatus`, `StepType`, `ProviderErrorCode`
- Asset fields: `width`, `height`, `duration`
- Step fields: `step_type`, `model_version`, `model_hash`, `seed`, `inputs`, `provider_payload`, `retries`, `error_code`
- Run fields: `status`, `project_id`
- Manifest fields: `manifest_uri`, `encryption_scheme`, `signature`
- `EmbedPolicy` model with `Manifest.to_embed_json()` for redaction
- PNG media handler (iTXt chunk embedding)
- JPEG and WebP media handlers (XMP-based embedding)
- Sidecar media handler (JSON alongside media)
- `SmartEmbedder` with auto-fallback to sidecar
- `get_handler()` media handler registry
- `MediaCapability` dataclass for handler introspection
- `Runnable[In, Out]` ABC with `|` composition operator
- `BaseProvider` with submit/poll/fetch_output lifecycle
- `ReplicateProvider` adapter (stores `provider_payload`)
- `genblaze-openai` `DalleProvider` expanded to OpenAI's full image lineup: `gpt-image-2` (free-form sizing), `gpt-image-1.5`, `gpt-image-1`, `gpt-image-1-mini`, alongside `dall-e-3` / `dall-e-2`
- `/v1/images/edits` support in `DalleProvider` — routed automatically when `step.inputs` is non-empty; accepts `file://` and `https://` inputs, optional `mask`, and multi-image composites
- New param passthroughs in `DalleProvider`: `output_format` (png/jpeg/webp), `output_compression`, `moderation`, `input_fidelity`, `mask`
- Registry-driven model metadata (`_ImageModelSpec`) replaces scattered size/pricing/format dicts; unknown models (`chatgpt-image-latest`, dated snapshots) pass through with `cost_usd=None`
- Provider error classification via `ProviderErrorCode`
- `Pipeline` fluent API for multi-step generation
- `PipelineResult` with `.save()` method and tuple unpacking support
- Pipeline: `step_type` parameter, `sink` parameter on `.run()`
- `ParquetSink` for structured run data output
- Parquet sink split into `runs/`, `steps/`, `assets/` tables with idempotency
- Parquet partitioning by `dt=/tenant_id=/modality=/provider=`
- `pyarrow` as optional `[parquet]` extra
- `StepSpan` timing context manager with `run_id`, `step_id`, `retries`, `cost` fields
- `StructuredLogger` for JSON log events; `.with_context()` for correlation IDs
- CLI: `extract`, `verify`, `replay`, `index` commands
- Builder methods: `.step_type()`, `.seed()`, `.model_version()`, `.model_hash()`, `.input_asset()`, `.project()`, `.status()`
- JSON Schema specifications (v1)
- GitHub Actions CI with Python 3.11/3.12/3.13 matrix
- Pre-commit hooks configuration
- `py.typed` PEP 561 marker
- Code coverage configuration (70% minimum)
- Shared test fixtures via `conftest.py`
- README.md with install, quickstart, and architecture docs
