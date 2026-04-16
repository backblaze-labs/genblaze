# Agent Standards Checklist

Verify the repo is optimized for AI agent consumption.
Mark `[x]` when verified, `[!]` when a problem is found.

## Discoverability
- [ ] `CLAUDE.md` provides correct read order and commands
- [ ] `AGENTS.md` serves as complete table of contents
- [ ] Doc map covers every documentation file
- [ ] `README.md` has clear install + quickstart sections
- [ ] Feature docs are named consistently and descriptively

## Type System
- [ ] `py.typed` marker present in `genblaze_core/`
- [ ] All public functions have full type annotations
- [ ] All public classes have type annotations
- [ ] Generic types are properly parameterized
- [ ] Return types are explicit (no implicit `-> None`)
- [ ] Union types use `X | Y` syntax (Python 3.11+)

## API Surface
- [ ] `__init__.py` exports all and only public symbols
- [ ] `__all__` is defined in all public modules
- [ ] No private symbols (prefixed `_`) leak into public API
- [ ] Builder methods return `Self` for chaining
- [ ] Consistent naming: `create_*`, `get_*`, `from_*` patterns

## Error Messages
- [ ] Exceptions include what operation failed
- [ ] Exceptions include what to try instead
- [ ] Validation errors reference the field that failed
- [ ] Timeout errors include the timeout value and operation
- [ ] Provider errors include the provider name and step

## JSON Schema Compliance
- [ ] Schemas in `libs/spec/schemas/` match Pydantic model serialization
- [ ] Schema versions are tracked
- [ ] Schemas are valid JSON Schema (draft-07 or later)
- [ ] All required fields marked correctly
- [ ] Enum schemas match Python Enum values

## Determinism & Reproducibility
- [ ] Canonical JSON produces identical output for identical input
- [ ] Hash computation is deterministic (SHA-256)
- [ ] UUID generation uses proper v4 random UUIDs
- [ ] No timestamp-dependent behavior in serialization
- [ ] Float normalization is consistent

## Test Utilities
- [ ] `genblaze_core.testing` provides fake providers for downstream use
- [ ] Test fixtures are importable and documented
- [ ] Example test patterns are shown in docs
- [ ] Mocking guidance for provider adapters exists

## Configuration
- [ ] All config is explicit (no hidden env vars)
- [ ] Defaults are sensible and documented
- [ ] Config validation happens at construction time
- [ ] Config errors are caught early with clear messages

## Multi-Agent Support
- [ ] `.claude/worktrees/` directory exists for agent branching
- [ ] `.claude/settings.local.json` has appropriate permissions
- [ ] Commands are non-interactive (no prompts, no stdin)
- [ ] `make test` and `make lint` are idempotent and parallelizable
- [ ] No global state that would conflict between concurrent agents
