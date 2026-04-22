---
name: verify-docs
description: Audit docs/* and root markdown for staleness. Checks `last_verified` headers against git mtime, validates Python code examples parse, checks cross-reference links resolve. Use monthly, before release, or after any large refactor.
allowed-tools: Read Grep Glob Bash(git log:*) Bash(python3:*)
---

# Verify docs freshness

Run the four checks below and produce a single report. Do NOT auto-fix — surface findings for
human review (stale docs usually need a human to decide whether behavior is still accurate).

## 1. Stale `last_verified` stamps

Every canonical doc in this repo carries `<!-- last_verified: YYYY-MM-DD -->` near the top.

- Glob: `README.md`, `ARCHITECTURE.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`,
  `docs/**/*.md`.
- For each file, extract the stamp. Flag as **P1 stale** when:
  - Stamp is absent (canonical docs must have one), OR
  - Stamp is >60 days old, OR
  - Stamp date is earlier than the most recent commit touching the file's *owning module*
    per the Doc Update Mapping in `docs/dev-workflows.md`.

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

- Grep markdown files for local links: `\[.+?\]\(\./.+?\)`, `\[.+?\]\(\.\./.+?\)`,
  `\[.+?\]\(docs/.+?\)`.
- For each match, resolve relative to the link's source file. Flag unresolved.

## 4. Feature-doc ↔ canonical-file alignment

`ARCHITECTURE.md` has a "Canonical Files" section mapping concepts to paths. Confirm each path
listed there exists. Flag any `libs/core/genblaze_core/...` reference that no longer resolves
(a common rot pattern after refactors).

## Report format

```
## Docs Audit — YYYY-MM-DD

### P1 — Stale `last_verified`
- docs/features/pipeline.md — stamp 2026-01-10, module last touched 2026-03-22

### P2 — Broken example syntax
- docs/features/webhooks.md — block 3: SyntaxError at line N

### P3 — Broken cross-links
- README.md → docs/features/queue-integration.md: target not found

### P4 — Missing canonical file
- ARCHITECTURE.md references libs/core/genblaze_core/old_module.py: not present
```
