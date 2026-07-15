<!-- completed: 2026-07-14 -->
# Provenance & Manifest Integrity Batch

## Summary

Batch fix for six provenance/manifest-integrity issues: validate manifests
consistently on load, reject impossible `Asset` metadata, make `ParquetSink`
idempotent across content changes, and harden `replay`'s CLI error handling.

## Scope

- **#78** — Add Pydantic field constraints to `Asset`, `VideoMetadata`,
  `AudioMetadata`, `WordTiming` rejecting negative/impossible numeric
  provenance fields and malformed `media_type` at construction. `sha256`
  stays construction-tolerant by design (#100); `Manifest.verify()` is the
  enforcement boundary for hash shape.
- **#72** — Make `ParquetSink.write_run()` idempotent when a run's content
  (and therefore its derived partition) changes between sinks: check the
  current partition first, fall back to a full-tree probe only on a miss,
  and remove a stale partition's `steps`/`assets` files before its `runs`
  sentinel so an interrupted cleanup is retryable.
- **#43** — Fix six `str | None` flow bugs in `cli/genblaze_cli/commands/replay.py`
  by narrowing `step.provider` before use; provider-less steps now raise a
  clear `ClickException` instead of a `TypeError`.
- **#64** — Add `dir_okay=False` to all four CLI file arguments; guard
  `parse_manifest()` against non-dict top-level JSON.
- **#50** — Add unit tests for the untested canonical-hash normalization
  branches (NaN/Inf, `Enum.value`, naive-datetime rejection, timezone
  canonicalization).
- **#73** — Investigated; already fixed on `main` since v0.2.2 (`d73c577`)
  with existing regression tests. No change made.

## Review

Three independent reviewers (correctness/tests, architecture/DRY,
data-integrity) examined the diff before push. Two follow-up fixes came out
of that pass: `ParquetSink`'s idempotency check was reordered to avoid a
full-tree scan on every write (the common case), and stale-partition
deletes were reordered (`steps`/`assets` before `runs`) so an interrupted
cleanup can't orphan files with no sentinel pointing at them.

## Verification

- `libs/core`: `pytest tests/ -q` (1869 passed; one pre-existing,
  environment-only numpy/pyarrow ABI failure unrelated to this batch)
- `cli`: `pytest tests/ -q` (31 passed, 7 new; pre-existing Click/pytest
  `CliRunner` version-mismatch failures unrelated to this batch)
- `mypy cli/genblaze_cli/ --ignore-missing-imports` — clean
- All 13 connector suites + `libs/meta` + `tools/tests` — all pass
- `ruff check` / `ruff format --check` — clean

PR: https://github.com/backblaze-labs/genblaze/pull/145
