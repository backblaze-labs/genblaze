---
name: verify-docs
description: Audit docs/* and root markdown for staleness. Runs the mechanical docs lint (frontmatter, stale_after, links), then the judgment checks — owning-module drift, Python example syntax, canonical-file paths. Use monthly, before release, or after any large refactor.
allowed-tools: Read Grep Glob Bash(git log:*) Bash(python3:*) Bash(python:*)
---

# Verify docs freshness

Run the checks below and produce a single report. Do NOT auto-fix — surface findings for
human review (stale docs usually need a human to decide whether behavior is still accurate).

## 0. Mechanical lint

Run `python tools/check_docs.py` (whole tree). It owns frontmatter validity, `stale_after`
freshness and link resolution, so don't re-implement those here. Copy its errors and stale-doc
warnings into the report under P0/P1/P3.

## 1. Stale docs

One freshness rule per doc:

- **Doc has OKF frontmatter** (`docs/**`, once edited): `stale_after` decides. Check 0 already
  flags a passed date. Additionally flag **P1** when the owning module (table below) has
  commits newer than the doc's last commit — the date hasn't passed but the code moved.
- **Legacy doc with `<!-- last_verified: YYYY-MM-DD -->`** (root files, untouched `docs/**`):
  flag **P1 stale** when the stamp is absent, >60 days old, or earlier than the most recent
  commit touching the owning module.

Owning-module lookup (from `docs/dev-workflows.md`):

| Doc | Owning module |
|---|---|
| `docs/features/pipeline.md` | `libs/core/genblaze_core/pipeline/` |
| `docs/features/provider-system.md` | `libs/core/genblaze_core/providers/` |
| `docs/features/media-embedding.md` | `libs/core/genblaze_core/media/` |
| `docs/features/object-storage.md` | `libs/core/genblaze_core/storage/` + `libs/connectors/s3/` |
| `docs/features/parquet-sink.md` | `libs/core/genblaze_core/sinks/` |
| `docs/features/manifest-provenance.md` | `libs/core/genblaze_core/canonical/` + `libs/core/genblaze_core/models/manifest.py` |
| `docs/features/cli.md` | `cli/` |
| `ARCHITECTURE.md` | whole repo — use root mtime |
| `docs/app-workflows.md` | `libs/core/` |
| `docs/dev-workflows.md` | `Makefile` + `.github/workflows/` |

Use `git log -1 --format=%cs -- <path>` to get last-touched date for a module.

## 2. Python example syntax

Every Python fenced block in `docs/features/*.md` should at minimum be parseable.

- For each code block (``` ```python ... ``` ```), write to a temp file and run
  `python3 -c "import ast; ast.parse(open(p).read())"`.
- Report parse errors only. Ignore import-resolution failures (examples use real API keys that
  aren't available in the audit env).

## 3. Cross-reference link integrity

Covered by check 0 (`broken link:` findings). Report them under P3.

## 4. Feature-doc ↔ canonical-file alignment

`ARCHITECTURE.md` has a "Canonical Files" section mapping concepts to paths. Confirm each path
listed there exists. Flag any `libs/core/genblaze_core/...` reference that no longer resolves
(a common rot pattern after refactors).

## Report format

```
## Docs Audit — YYYY-MM-DD

### P0 — Docs lint errors (invalid frontmatter, missing required files)
- docs/features/x.md — stale_after must be ISO 8601 with a UTC offset

### P1 — Stale (`stale_after` passed, legacy stamp old, or owning module moved)
- docs/features/pipeline.md — stamp 2026-01-10, module last touched 2026-03-22

### P2 — Broken example syntax
- docs/features/webhooks.md — block 3: SyntaxError at line N

### P3 — Broken cross-links
- README.md → docs/features/queue-integration.md: target not found

### P4 — Missing canonical file
- ARCHITECTURE.md references libs/core/genblaze_core/old_module.py: not present
```
