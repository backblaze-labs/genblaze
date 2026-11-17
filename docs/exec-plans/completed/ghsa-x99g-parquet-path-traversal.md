---
type: Plan
title: GHSA-x99g-wcm2-r2v8, path-unsafe identity fields in ParquetSink
description: Completed record of the GHSA-x99g fix (identity-field validation and ParquetSink containment), its design decisions and outcome.
status: stable
stale_after: 2027-05-17T00:00:00Z
owner: "@jeronimodeleon"
tags: [security, core, parquet]
---

# GHSA-x99g-wcm2-r2v8: path-unsafe identity fields in ParquetSink

**Outcome:** fixed in #328, released in v0.8.0 (`genblaze-core` 0.3.9).
Advisory [GHSA-x99g-wcm2-r2v8](https://github.com/backblaze-labs/genblaze/security/advisories/GHSA-x99g-wcm2-r2v8)
published 2026-11-16 (CWE-22, medium; affected `genblaze-core <= 0.3.8`).

## Problem

`ParquetSink` joined `Run.run_id` into filesystem paths without validation,
reachable from `genblaze index` on an untrusted manifest. Identity fields are
excluded from the canonical hash, so a tampered `run_id` still verified. The
same unvalidated-ID pattern existed in connector `output_dir` writes and in
storage object keys.

## Decisions

- **One check, applied at the model layer.** `validate_path_component`
  (`genblaze_core/_utils.py`) rejects empty values, `.`/`..`, `/`, `\`, NUL
  and `:`. Pydantic validators apply it to `Run.run_id`, `Run.parent_run_id`,
  `Run.tenant_id`, `Step.step_id`, `Step.run_id` and `Asset.asset_id`, so every
  manifest load is covered. `get_output_path` uses the same check.
- **Not strict UUID.** Fixtures and documented examples use non-UUID IDs
  (`"intro clip"`, `"v1.2"`). Blocking separators and dot segments closes the
  traversal without breaking them. The JSON/TS spec still declares
  `format: uuid`; that divergence is documented, not changed.
- **`..` is rejected,** even though `get_output_path` previously accepted it,
  because HIERARCHICAL object keys use `run_id` as a bare segment.
- **`tenant_id` allows `:`** (`allow_colon=True`). It never reaches a
  filesystem path raw (ParquetSink re-sanitizes partitions), and URN-style
  tenants are legitimate. The plan had proposed rejecting `:` everywhere.
- **Validation at construction only.** `Run`/`Step` have no
  `validate_assignment`, and the one post-construction assignment
  (`RunBuilder.build` setting `step.run_id`) copies an already-validated value.
- **ParquetSink containment as defense in depth.** Every write and unlink
  target must stay under `base_dir`. It is not redundant: partitions are read
  back from on-disk `.partition` files, which the model validator never sees.
  The check is lexical (`normpath`), not `resolve()`, so symlinks the owner
  placed inside `base_dir` keep working.
- No canonical hashes change, since identity fields are hash-excluded.

## Tests

- Model: each identity field rejects `../x`, `a/b`, `a\b`, `/abs`, `C:x`,
  NUL and `..`, and accepts `r1`, `intro clip`, `v1.2` and UUIDs
  (`test_identity_path_safety.py`).
- Hash: a tampered `run_id` keeps its hash but fails to parse.
- CLI: `genblaze index` and `genblaze verify` on a hostile manifest exit with a
  clean error and write nothing outside `-o` (`cli/tests/test_cli.py`).
- ParquetSink: a tampered `.partition` file cannot trigger an unlink outside
  `base_dir` (`test_parquet.py`).

## Compatibility

Manifests with `/`, `\`, `..` or (outside `tenant_id`) `:` in an identity field
no longer parse. Such manifests could not be sunk safely anyway. The error
names the field, and the CHANGELOG calls out the change.

## Related changes

- #334: connector output files are named through `get_output_path`, with a
  repo-wide guard test, so connectors share the same rule set.
- #335: `urllib3>=2.8.0` floor, shipped in the same wave.
- User-facing docs: [ParquetSink](../../features/parquet-sink.md).
