---
type: Library
title: CLI
description: The genblaze command for extracting, verifying, replaying and indexing manifests.
status: stable
stale_after: 2027-05-18T00:00:00Z
owner: "@jeronimodeleon"
---
# Feature: CLI

## Purpose
Command-line tools to extract, verify, replay, and index manifests from media files.

## Used By
- CLI: `genblaze` command (click-based)

## Core Functions
- `extract` — Extract and display manifest from any supported media file (auto-detects format)
- `verify` — Verify manifest hash integrity and output `sha256` coverage from media, sidecar JSON, or standalone manifest JSON (exit code 0=OK, 1=failed verification); the opt-in `--fetch` mode also downloads each output asset and re-hashes its bytes against the declared digest, and `--allowed-root <dir>` (repeatable) admits `file://` assets outside the temp-dir allowlist, which you need when your pipeline wrote assets via `output_dir=` to a non-default location
- `replay` — Preview (`--dry-run`) or re-execute (`--no-dry-run`) a pipeline from manifest JSON
- `index` — Write manifest data to a Parquet sink (needs `genblaze-core[parquet]`)

## Canonical Files
- CLI entry: `cli/genblaze_cli/main.py`
- Extract command: `cli/genblaze_cli/commands/extract.py`
- Verify command: `cli/genblaze_cli/commands/verify.py`
- Replay command: `cli/genblaze_cli/commands/replay.py`
- Index command: `cli/genblaze_cli/commands/index.py`

## Inputs
- `extract <file> [--format json|summary] [-o/--output <file>]` — Media file path; `--format` defaults to `json`; `-o` writes the manifest JSON to a file instead of stdout (the summary always goes to stdout)
- `verify <file> [--hash-only | --fetch [--allowed-root <dir>]...]` — Media file path, direct `*.genblaze.json` sidecar, or standalone `manifest.json` file. JSON suffix matching is case-insensitive. `--hash-only` checks only `canonical_hash` and skips output `sha256` and asset-metadata checks; it is mutually exclusive with `--fetch`, and `--allowed-root` requires `--fetch`
- `replay <manifest.json> [--dry-run/--no-dry-run] [--allow-provider <name>]... [--show-prompts] [--force]` — `--allow-provider` (repeatable) skips the per-provider confirmation prompt for the named providers; `--show-prompts` prints public-visibility prompts in the summary (non-public prompts stay redacted); `--force` skips the manifest hash check
- `index <manifest.json> [-o/--output <dir>]` — Manifest JSON + output directory (default `./genblaze_index`). Requires pyarrow: `pip install 'genblaze-core[parquet]'`

## Outputs
- `extract` → manifest JSON to stdout or the `-o` file; `--format summary` prints run ID, step count, hash and verification status
- `verify` → exit code (0 or 1)
- `replay` → dry-run summary or re-executed pipeline
- `index` → Parquet files in output directory

## Flow
- User invokes `genblaze <command> <args>`
- Click routes to command handler
- Commands use core library (media handlers, manifest verify, ParquetSink)

## Edge Cases
- File without manifest → extract/verify report "no manifest found"
- Unsupported format → tries sidecar fallback
- Corrupted hash → verify exits with code 1
- Missing, uppercase, or malformed output `sha256` → verify exits with code 1
- Standalone JSON manifests enforce `MAX_MANIFEST_BYTES` before reading
- Pointer-mode sidecars passed directly produce an actionable pointer-sidecar error
- A bare `verify` does not fetch `asset.url`; the opt-in `--fetch` mode closes that gap by streaming bytes through the SSRF-pinned transfer path and comparing them to `asset.sha256`. Fetched bytes prove integrity at fetch time, not forever after; a mutable URL can serve different bytes tomorrow
- Replay dry-run (default) → no API calls made
- Replay `--no-dry-run` → requires provider package installed (e.g., `genblaze-replicate`)
- Unknown provider in manifest → error with list of known providers
- Provider-less step (`INGEST`/`IMPORT`, `provider=None`) → replay
  `--no-dry-run` raises a clear error naming the step; not yet supported
- Directory passed where a file is expected (`extract`/`verify`/`index`/`replay`)
  → clear "is a directory" error, not a downstream crash
- Manifest JSON whose top-level value isn't an object (e.g. a JSON array)
  → same clean `ManifestError` from `index` and `replay`

## Verification
- Test files: `cli/tests/test_cli.py`, `cli/tests/test_verify_fetch.py`
- Required cases: extract from PNG, verify pass/fail, direct JSON and sidecar JSON inputs, oversized standalone JSON rejection, missing or malformed output `sha256`, replay dry-run, `--fetch` byte match/mismatch, `--allowed-root` admission
- Quick verify: `cd cli && pytest tests/ -v`
- Full verify: `make test`
- Pass criteria: all CLI commands handle happy path and error cases
