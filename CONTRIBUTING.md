# Contributing to genblaze

Thanks for your interest in contributing! This guide will help you get started.

## Development setup

```bash
git clone https://github.com/<your-username>/genblaze.git
cd genblaze
make install-dev
make test          # Verify setup
```

## Project structure

```
libs/core/              # genblaze-core Python SDK
libs/connectors/        # Provider adapters (replicate/)
libs/spec/              # Language-neutral JSON Schemas
cli/                    # CLI tool
examples/               # Usage examples
docs/                   # Internal feature docs
```

## Making changes

1. **Read the docs first**: [ARCHITECTURE.md](ARCHITECTURE.md) for system layout, [AGENTS.md](AGENTS.md) for conventions.
2. **Plan larger work.** Maintainers write an execution plan for multi-file changes or new features in `docs/exec-plans/active/<feature>/plan.md`. External contributors: open or link a GitHub issue describing the change instead.
3. **Write tests** for new functionality. Don't skip them.
4. **Update docs** in the same PR as code changes (see [Doc Update Mapping](docs/dev-workflows.md)).

### Adding a new provider

See [docs/guides/new-provider.md](docs/guides/new-provider.md) for a complete walkthrough — package setup, base class selection (`SyncProvider` for most APIs, `BaseProvider` for polling), entry points, error handling, asset validation, and the compliance test harness.

## Running checks

```bash
make verify     # The done check: lint + typecheck + tests + docs lint + e2e smoke
make test       # Full test suite
make lint       # Ruff linter + format check
make fmt        # Auto-format code
make typecheck  # mypy type checking
make coverage   # Tests with coverage report (70% minimum)
make ts-types   # Regenerate libs/spec/ts/genblaze.d.ts from JSON Schemas
```

On a fork, `origin` is your fork, so point the docs lint at upstream:
`git fetch upstream main && make verify DOCS_BASE=upstream/main`.

If you change a Pydantic model in `libs/core/genblaze_core/models/`,
update the matching schema in `libs/spec/schemas/manifest/v1/` and run
`make ts-types`. CI's `ts-types` job will fail the PR otherwise.

## Pull request process

1. `make verify` passes; paste its output into the PR's **Testing** section
2. Docs updated in the same PR as code changes. New or edited files under
   `docs/` need OKF frontmatter (see [AGENTS.md](AGENTS.md#docs-and-plans));
   `make docs-lint` checks it
3. Fill in the PR template: Testing, Risk, How to revert, Public API / semver
4. Reference the issue or execution plan
5. Every PR check passes on a fork with no secrets. Reviews follow
   [REVIEW.md](REVIEW.md)
6. If your change should ship in a release, add a bullet to the
   `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md) under the
   appropriate package heading. The release workflow's `changelog-gate`
   fails any release where `[Unreleased]` still contains entries — so
   you _will_ remember to write release notes.

## Releases

Maintainer-facing. If you're cutting a release, read
[RELEASING.md](RELEASING.md) — it documents the wave-naming convention,
the gates that must pass before any package is built, the order in
which packages publish (core → cli/connectors → meta umbrella), the
dry-run path, and how to recover from a partially-failed
release. The release-smoke gate (`make release-smoke`) is the same
script the CI workflow runs; run it locally before tagging.

## Internal context (Backblaze maintainers)

This repo is public. Internal tickets, links and specs live in a private
companion repo cloned into the gitignored `.internal/` folder:

```bash
git clone <companion-repo-url> .internal
echo "@.internal/AGENTS.md" > CLAUDE.local.md
```

In a git worktree, symlink rather than re-clone: `ln -s <main-checkout>/.internal .internal`
(`CLAUDE.local.md` is copied automatically via `.worktreeinclude`). Never
copy ticket keys or internal URLs into tracked files, branch names, PR titles
or commit messages. The `Docs` workflow fails on Atlassian links; its Jira-key
and host lists are pending, so check the rest by hand. Link the public PR
from the ticket instead.

## Code conventions

- Python 3.11+ required
- Pydantic v2 models only
- All IDs are UUIDs
- Match existing code style — ruff enforces formatting
- Keep diffs minimal — only change what's needed
- Canonical JSON hashing must remain deterministic

## Test placement

| Type | Location |
|------|----------|
| Core unit tests | `libs/core/tests/unit/` |
| Golden tests (round-trip) | `libs/core/tests/golden/` |
| CLI tests | `cli/tests/` |
| Shared fixtures | `libs/core/tests/conftest.py` |

## Reporting bugs

Use [GitHub Issues](https://github.com/backblaze-labs/genblaze/issues) with the bug report template. Include:
- What you expected vs. what happened
- Minimal reproduction steps
- Python version and OS

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be respectful and constructive.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
