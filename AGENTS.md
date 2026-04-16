<!-- last_verified: 2026-04-16 -->
# Agents

## Repo Purpose

Orchestration framework for generative media pipelines with manifest-based provenance tracking. Produces 13 pip-installable packages: `genblaze-core`, 10 provider adapter packages (`genblaze-openai`, `genblaze-google`, `genblaze-runway`, `genblaze-luma`, `genblaze-decart`, `genblaze-replicate`, `genblaze-elevenlabs`, `genblaze-stability-audio`, `genblaze-lmnt`, `genblaze-gmicloud`), `genblaze-s3`, and `genblaze-cli`.

## Architecture Boundaries

- `libs/core/` — Core SDK; no external API dependencies
- `libs/connectors/replicate/` — Replicate-specific; depends on core
- `cli/` — CLI commands; depends on core
- `libs/spec/` — Language-neutral JSON schemas; no Python dependencies
- Providers never store API tokens in manifests
- See [ARCHITECTURE.md](ARCHITECTURE.md) for full detail

## Invariants and Guardrails

- All changes must pass `make test` before PR
- Canonical JSON hashing must remain deterministic — never change key sort order or float normalization
- Manifest `canonical_hash` must always verify against re-serialized content
- Provider adapters must implement `submit/poll/fetch_output` — no exceptions
- All IDs are UUIDs — never sequential integers
- `EmbedPolicy` must be respected in all embedding paths
- Pydantic v2 models only — no v1 compatibility layer
- Docs must be updated in the same PR as code changes (see [Doc Update Mapping](docs/dev-workflows.md))
- Python 3.11+ required

## Doc Map

- [README.md](README.md) — Product overview, install, quickstart
- [ARCHITECTURE.md](ARCHITECTURE.md) — System layout, data flows, canonical files
- [AGENTS.md](AGENTS.md) — This file; agent table of contents
- [docs/features/](docs/features/) — Feature docs (one per core feature)
- [docs/app-workflows.md](docs/app-workflows.md) — User journeys
- [docs/dev-workflows.md](docs/dev-workflows.md) — Engineering workflows
- [docs/exec-plans/active/](docs/exec-plans/active/) — Active execution plans
- [docs/exec-plans/completed/](docs/exec-plans/completed/) — Completed plans
- [docs/exec-plans/tech-debt-tracker.md](docs/exec-plans/tech-debt-tracker.md) — Known tech debt
- [CLAUDE.md](CLAUDE.md) — Claude Code agent config

## Planning and Execution

- Plans live in `docs/exec-plans/active/`; move to `completed/` when done
- Plans required for: multi-file changes, new features, refactors
- See [docs/dev-workflows.md](docs/dev-workflows.md) for full process

## Review Loop

- Run `make test` — all tests must pass
- Run `make lint` — no linter errors
- Update relevant feature docs and ARCHITECTURE.md if behavior changed
- PR must reference the execution plan if one exists
