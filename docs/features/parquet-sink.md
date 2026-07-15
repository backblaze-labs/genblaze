<!-- last_verified: 2026-07-15 -->
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
- `_run_index/`: internal `run_id -> partition` bookkeeping (one small file
  per `run_id`); not a data table, ignored by Hive-style dataset readers
  scanning `runs/`/`steps`/`assets`

## Flow
- `ParquetSink(output_dir, policy=None)` initializes with target directory
  and optional `EmbedPolicy`. If `output_dir` already has a `runs/` tree but
  no `_run_index/` yet (e.g. first use after upgrading), the index is
  backfilled once from that tree.
- `write_run()` flattens run/steps/assets into tabular rows
- When `policy` is set, redacts prompt/params/seed per `EmbedPolicy` rules before writing step rows
- Writes to partitioned Parquet using pyarrow
- Idempotent by `run_id`: the partition is derived from run content (step
  modality/provider set), so it can move between sinks of the same `run_id`
  (e.g. a resume that completes more steps).
  - Same partition as the last sink: compares the new manifest's
    `canonical_hash` against the sentinel already on disk. Identical content
    is a no-op — but the `run_id -> partition` index entry is refreshed even
    on this path, so a prior crash that wrote the sentinel but never
    persisted its index entry self-heals on the next call rather than only
    a future rewrite. Changed content (e.g. a resume that added steps
    without changing modality/provider) rewrites the `runs`/`steps`/`assets`
    rows in place instead of silently dropping the new data (#152).
  - Different partition than the last sink: the `run_id -> partition` index
    (`_run_index/`) is consulted — an O(1) lookup, not a scan of the whole
    `runs/` tree (#150) — to find and remove the stale partition's
    `steps`/`assets`/`runs` files (in that order) before writing the fresh
    ones, so a completed write leaves exactly one row set per `run_id` (#72).
    The index itself is backfilled once, atomically (temp directory +
    `os.replace`), from any pre-existing `runs/` tree that predates it.
  - This is not a cross-file transaction: a crash between removing the stale
    sentinel and writing the new one leaves the run temporarily un-sinked
    until the next `write_run()` call for that `run_id`, the same accepted
    trade-off as the original first-write completion sentinel. A crash
    between writing the new sentinel and persisting its index entry is
    self-healed by that same next call (including a no-op one, per above);
    only a crash there immediately followed by a *second* partition move
    before any intervening call remains an accepted, narrow residual risk.

## Edge Cases
- Duplicate `run_id`, same content → write skipped (idempotent), index entry refreshed
- Duplicate `run_id`, changed content, same partition → rewritten in place (#152)
- Duplicate `run_id`, changed content, moved partition → stale partition's
  files are replaced by the new write, not duplicated (#72)
- New `run_id` in a dataset with many existing partitions → resolved via the
  `_run_index/` file for that `run_id`, never a full-tree scan (#150)
- Missing pyarrow → `ImportError` at sink creation (optional dependency)
- Concurrent writes to the *same* `ParquetSink` instance → thread-safe via internal lock
- Concurrent writes to *different* `run_id`s from separate `ParquetSink`
  instances (e.g. parallel `genblaze index` invocations into one output
  dir) → each `run_id`'s index entry is its own file, so they don't race
- `EmbedPolicy` with `prompt_visibility=PRIVATE` → prompts written as empty strings
- `EmbedPolicy` with `include_params=False` → params written as `{}`
- `EmbedPolicy` with `include_seed=False` → seed written as null
- Re-sinking a run under a *changed* `EmbedPolicy` only (run content
  otherwise identical) → still a no-op: `canonical_hash` reflects
  provenance content, not policy, so already-sunk rows keep their original
  redaction. Re-sink under a policy change is not currently a supported way
  to purge already-written prompt/params/seed values.

## Verification
- Test files: `libs/core/tests/unit/test_parquet.py`
- Required cases: write + read round-trip, idempotency, partition structure
- Quick verify: `cd libs/core && pytest tests/unit/test_parquet.py -v`
- Full verify: `make test`
- Pass criteria: Parquet files created with correct partitions, idempotent re-writes
