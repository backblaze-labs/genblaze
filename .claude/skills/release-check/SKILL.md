---
name: release-check
description: Pre-release gate for one genblaze package — version bump, tests, lint, typecheck, CHANGELOG entry, entry-point sanity. Use before creating a GitHub Release (which triggers the PyPI publish workflow in .github/workflows/release.yml).
argument-hint: <package> <new-version>
allowed-tools: Bash(make:*) Bash(pytest:*) Bash(ruff check:*) Bash(ruff format:*) Bash(mypy:*) Bash(pip:*) Bash(python:*) Bash(python3:*) Bash(twine check:*) Bash(git:*) Read Grep Edit
---

# Release check — $ARGUMENTS

The release workflow is triggered by a published GitHub Release (see `.github/workflows/release.yml`).
`publish-core` runs first, then `publish-cli` and `publish-connectors` in parallel. Every package
needs its own version bump + CHANGELOG entry + passing tests before you tag.

Parse `$ARGUMENTS` as `<package> <new-version>` (e.g. `core 0.3.2`, `openai 0.2.0`).

## Locate the package

| Package | Path |
|---|---|
| `core` | `libs/core` |
| `cli` | `cli` |
| any connector | `libs/connectors/<name>` |

## Checklist

Run each step. Stop and report on the first failure.

1. **Version bump.** Read `<path>/pyproject.toml`. If `version` != `$2`, edit it to `$2`.
2. **Core dependency pin (connectors only).** Confirm `genblaze-core>=<x>` is at or above the
   currently published core version. If `core` is also being released in this wave, the
   connector pin should match.
3. **Entry point sanity (connectors only).** Confirm the
   `[project.entry-points."genblaze.providers"]` line exists and the module/class is importable:
   `python -c "import genblaze_<name>; <name>.<Class>()" || true` (instantiation may require
   credentials — just check the import).
4. **Tests.** `cd <path> && pytest -v` must pass.
5. **Lint.** `ruff check <path>` and `ruff format --check <path>` must pass.
6. **Typecheck (core only; connectors optional).** `mypy <path>/genblaze_*/ --ignore-missing-imports`.
7. **Build check.** `cd <path> && python -m build && twine check dist/*` — validates wheel metadata
   before PyPI sees it.
8. **CHANGELOG.** Confirm `CHANGELOG.md` has an entry for version `$2` scoped to this package.
   If missing, draft one from `git log --oneline $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~30)..HEAD -- <path>`.
9. **Doc freshness.** For connectors: confirm the matching section in README.md and any
   `docs/features/provider-system.md` entry is accurate for `$2`.

## Report

A pass/fail table, then the exact next commands:

```
git add <path>/pyproject.toml CHANGELOG.md
git commit -m "release: <package> v<new-version>"
git push
gh release create <package>-v<new-version> --title "<package> v<new-version>" --notes-from-tag
```

Do NOT create the tag or release yourself — leave it for the user to review and trigger.
