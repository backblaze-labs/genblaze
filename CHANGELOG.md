# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
