# Contributing to genblaze

Thanks for your interest in contributing! This guide will help you get started.

## Development setup

```bash
git clone https://github.com/backblaze-b2-samples/genblaze.git
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
2. **Create an execution plan** for multi-file changes or new features — place it in `docs/exec-plans/active/`.
3. **Write tests** for new functionality. Don't skip them.
4. **Update docs** in the same PR as code changes (see [Doc Update Mapping](docs/dev-workflows.md)).

### Adding a new provider

See [docs/guides/new-provider.md](docs/guides/new-provider.md) for a complete walkthrough — package setup, base class selection (`SyncProvider` for most APIs, `BaseProvider` for polling), entry points, error handling, asset validation, and the compliance test harness.

## Running checks

```bash
make test       # Full test suite
make lint       # Ruff linter + format check
make fmt        # Auto-format code
make typecheck  # mypy type checking
make coverage   # Tests with coverage report (70% minimum)
```

## Pull request process

1. All tests pass (`make test`)
2. Linter passes (`make lint`)
3. Docs updated in the same PR as code changes
4. PR description summarizes what and why
5. Reference the execution plan if one exists

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

Use [GitHub Issues](https://github.com/backblaze-b2-samples/genblaze/issues) with the bug report template. Include:
- What you expected vs. what happened
- Minimal reproduction steps
- Python version and OS

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be respectful and constructive.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
