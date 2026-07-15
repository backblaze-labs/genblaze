<!-- last_verified: 2026-07-14 -->
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
- Idempotent by `run_id`: the partition is derived from run content
  (step modality/provider set), so it can move between sinks of the same
  `run_id` (e.g. a resume that completes more steps). A same-partition
  match is a no-op; a match under a *different* partition means content
  changed since the last sink — the stale partition's `runs`/`steps`/`assets`
  files are removed before writing the fresh ones, so exactly one row set
  exists per `run_id` at all times (#72)

## Edge Cases
- Duplicate `run_id`, same content → write skipped (idempotent)
- Duplicate `run_id`, changed content (moved partition) → stale partition's
  files are replaced by the new write, not duplicated
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
