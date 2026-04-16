<!-- last_verified: 2026-03-06 -->
# Feature: Parquet Sink

## Purpose
Write structured run/step/asset data to partitioned Parquet files for analytics and querying.

## Used By
- API: `ParquetSink`, `Pipeline.run(sink=...)`
- CLI: `index` command

## Core Functions
- `ParquetSink.write_run(run, manifest)` — Write run data to partitioned Parquet
- `BaseSink` — Abstract sink interface

## Canonical Files
- Parquet sink: `libs/core/genblaze_core/sinks/parquet.py`
- Sink base: `libs/core/genblaze_core/sinks/base.py`

## Inputs
- `Run` and `Manifest` objects
- `output_dir`: path for Parquet output

## Outputs
- Three Parquet table directories: `runs/`, `steps/`, `assets/`
- Partition scheme: `dt=YYYY-MM-DD/tenant_id={tenant}/modality={modality}/provider={provider}`

## Flow
- `ParquetSink(output_dir, policy=None)` initializes with target directory and optional `EmbedPolicy`
- `write_run()` flattens run/steps/assets into tabular rows
- When `policy` is set, redacts prompt/params/seed per `EmbedPolicy` rules before writing step rows
- Writes to partitioned Parquet using pyarrow
- Idempotent: skips if `{run_id}.parquet` already exists

## Edge Cases
- Duplicate `run_id` → write skipped (idempotent)
- Missing pyarrow → `ImportError` at sink creation (optional dependency)
- Concurrent writes → thread-safe via internal lock
- `EmbedPolicy` with `prompt_visibility=PRIVATE` → prompts written as empty strings
- `EmbedPolicy` with `include_params=False` → params written as `{}`
- `EmbedPolicy` with `include_seed=False` → seed written as null

## Verification
- Test files: `libs/core/tests/unit/test_parquet.py`
- Required cases: write + read round-trip, idempotency, partition structure
- Quick verify: `cd libs/core && pytest tests/unit/test_parquet.py -v`
- Full verify: `make test`
- Pass criteria: Parquet files created with correct partitions, idempotent re-writes
