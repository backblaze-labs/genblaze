# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
