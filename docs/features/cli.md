<!-- last_verified: 2026-03-06 -->
# Feature: CLI

## Purpose
Command-line tools to extract, verify, replay, and index manifests from media files.

## Used By
- CLI: `genblaze` command (click-based)

## Core Functions
- `extract` — Extract and display manifest from any supported media file (auto-detects format)
- `verify` — Verify manifest hash integrity and output `sha256` coverage from media, sidecar JSON, or standalone manifest JSON (exit code 0=OK, 1=failed verification)
- `replay` — Preview (`--dry-run`) or re-execute (`--no-dry-run`) a pipeline from manifest JSON
- `index` — Write manifest data to a Parquet sink

## Canonical Files
- CLI entry: `cli/genblaze_cli/main.py`
- Extract command: `cli/genblaze_cli/commands/extract.py`
- Verify command: `cli/genblaze_cli/commands/verify.py`
- Replay command: `cli/genblaze_cli/commands/replay.py`
- Index command: `cli/genblaze_cli/commands/index.py`

## Inputs
- `extract <file>` — Media file path
- `verify <file>` — Media file path, direct `*.genblaze.json` sidecar, or standalone `manifest.json` file. JSON suffix matching is case-insensitive.
- `replay <manifest.json>` — Manifest JSON file, `--no-dry-run` flag
- `index <manifest.json> -o <dir>` — Manifest JSON + output directory

## Outputs
- `extract` → manifest JSON to stdout
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
- `verify` does not fetch `asset.url` or re-hash remote bytes; callers that dereference URLs must compare fetched bytes to `asset.sha256`
- Replay dry-run (default) → no API calls made
- Replay `--no-dry-run` → requires provider package installed (e.g., `genblaze-replicate`)
- Unknown provider in manifest → error with list of known providers

## Verification
- Test files: `cli/tests/test_cli.py`
- Required cases: extract from PNG, verify pass/fail, direct JSON and sidecar JSON inputs, oversized standalone JSON rejection, missing or malformed output `sha256`, replay dry-run
- Quick verify: `cd cli && pytest tests/test_cli.py -v`
- Full verify: `make test`
- Pass criteria: all CLI commands handle happy path and error cases
