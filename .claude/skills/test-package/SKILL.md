---
name: test-package
description: Run tests for a single genblaze package or for only packages whose files changed since main. Use when iterating on a specific connector or after a focused edit — much faster than `make test` across all 13 packages.
argument-hint: <package | changed>
allowed-tools: Bash(pytest:*) Bash(cd:*) Bash(git diff:*) Bash(git status:*) Read
---

# Run targeted tests — $ARGUMENTS

Parse `$ARGUMENTS`:

- If `$ARGUMENTS` is `changed`:
  1. Run `git diff --name-only origin/main...HEAD` and `git diff --name-only` (working tree).
  2. Union the paths. For each path, map to its owning package:
     - `libs/core/**` → `core`
     - `cli/**` → `cli`
     - `libs/connectors/<name>/**` → `<name>`
     - `libs/spec/**` → `core` (schemas are consumed by core)
     - `docs/**`, `examples/**`, root-level configs → no test run (report and stop)
  3. Run tests for each affected package (see mapping below). Stop on first failure.

- Otherwise treat `$ARGUMENTS` as one package name. Valid values:
  - `core`, `cli`
  - Connectors: `openai`, `google`, `runway`, `luma`, `decart`, `replicate`, `elevenlabs`, `stability-audio`, `lmnt`, `gmicloud`, `s3`, `langsmith`

## Test command mapping

| Package | Command |
|---|---|
| `core` | `cd libs/core && pytest tests/ -v` |
| `cli` | `cd cli && pytest tests/ -v` |
| any connector `<name>` | `cd libs/connectors/<name> && pytest -v` |

## Quick subset (single file under core)

If the user hints at one file (e.g. "just the pipeline test"), run
`cd libs/core && pytest tests/unit/<file>.py -v` per the CLAUDE.md guidance.

## Report

- Pass/fail per package, with pytest's summary line.
- If any failure, surface the failing test name and the first traceback — do not proceed to other packages.
- Remind the user that `make test` is still the full-suite gate before PR.
