# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- New enums: `RunStatus`, `StepType`, `ProviderErrorCode`
- Asset fields: `width`, `height`, `duration`
- Step fields: `step_type`, `model_version`, `model_hash`, `seed`, `inputs`, `provider_payload`, `retries`, `error_code`
- Run fields: `status`, `project_id`
- Manifest fields: `manifest_uri`, `encryption_scheme`, `signature`
- `EmbedPolicy` model with `Manifest.to_embed_json()` for redaction
- JPEG and WebP media handlers (XMP-based embedding)
- `SmartEmbedder` with auto-fallback to sidecar
- `get_handler()` media handler registry
- `MediaCapability` dataclass for handler introspection
- `PipelineResult` with `.save()` method and tuple unpacking support
- Pipeline: `step_type` parameter, `sink` parameter on `.run()`
- Parquet sink split into `runs/`, `steps/`, `assets/` tables with idempotency
- Provider error classification via `ProviderErrorCode`
- `ReplicateProvider` stores `provider_payload`
- `StepSpan` fields: `run_id`, `step_id`, `retries`, `cost`
- `StructuredLogger.with_context()` for correlation IDs
- CLI commands: `replay`, `index`
- Builder methods: `.step_type()`, `.seed()`, `.model_version()`, `.model_hash()`, `.input_asset()`, `.project()`, `.status()`
- Unicode NFC normalization in canonical JSON
- GitHub Actions CI with Python 3.11/3.12/3.13 matrix
- Pre-commit hooks configuration
- `py.typed` PEP 561 marker
- Code coverage configuration (70% minimum)
- Shared test fixtures via `conftest.py`
- README.md with install, quickstart, and architecture docs

### Changed
- `pyarrow` moved to optional `[parquet]` extra
- Parquet partition keys changed to `dt=/tenant_id=/modality=/provider=`
- `Pipeline.run()` now returns `PipelineResult` (backward-compatible via `__iter__`)

## [0.1.0] - 2026-03-06

### Added
- Initial release
- Core models: `Asset`, `Step`, `Run`, `Manifest`
- Fluent builders: `StepBuilder`, `RunBuilder`, `ManifestBuilder`
- Canonical JSON serialization with SHA-256 hashing
- PNG media handler (iTXt chunk embedding)
- Sidecar media handler (JSON alongside media)
- `Runnable[In, Out]` ABC with `|` composition operator
- `BaseProvider` with submit/poll/fetch_output lifecycle
- `ReplicateProvider` adapter
- `ParquetSink` for structured run data output
- `Pipeline` fluent API for multi-step generation
- `StepSpan` timing context manager
- `StructuredLogger` for JSON log events
- CLI: `extract`, `verify` commands
- JSON Schema specifications (v1)
