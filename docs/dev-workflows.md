<!-- last_verified: 2026-04-23 -->
# Dev Workflows

## Setup

- [ ] Clone repo
- [ ] Run `make install-dev`
- [ ] Run `make test` to verify setup

## New Feature

- [ ] Create execution plan in `docs/exec-plans/active/`
- [ ] Implement feature in appropriate package (`libs/core/`, `libs/connectors/`, `cli/`)
- [ ] Write tests (unit at minimum)
- [ ] Create feature doc in `docs/features/<feature>.md`
- [ ] Update `ARCHITECTURE.md` if system layout changed
- [ ] Update `docs/app-workflows.md` if user journeys changed
- [ ] If a Pydantic wire model changed: update `libs/spec/schemas/manifest/v1/` and run `make ts-types`
- [ ] Run `make test` and `make lint`
- [ ] Move plan to `docs/exec-plans/completed/`

## Bugfix

- [ ] Add failing test that reproduces the bug
- [ ] Confirm test fails
- [ ] Implement fix
- [ ] Run `make test` — confirm all green
- [ ] Update feature doc if behavior changed

## Refactor

- [ ] Create execution plan in `docs/exec-plans/active/`
- [ ] Run `make test` before starting (baseline)
- [ ] Make changes incrementally
- [ ] Run `make test` after each change
- [ ] Update docs if interfaces changed

## Documentation Update

- [ ] Edit the canonical doc (feature doc, ARCHITECTURE.md, etc.)
- [ ] Update `<!-- last_verified: YYYY-MM-DD -->` header
- [ ] Verify cross-links are correct
- [ ] No duplicate information across docs

## Pull Request

- [ ] All tests pass (`make test`)
- [ ] Linter passes (`make lint`)
- [ ] Docs updated in same PR as code changes
- [ ] Execution plan referenced if applicable
- [ ] PR description summarizes changes

## Testing

### Test types
- **Unit**: Pure logic — models, builders, canonical JSON, policy
- **Golden**: Round-trip verification — PNG embed/extract
- **CLI**: Command integration — extract, verify, replay, index

### Test placement
- Core unit: `libs/core/tests/unit/`
- Core golden: `libs/core/tests/golden/`
- CLI: `cli/tests/`
- Shared fixtures: `libs/core/tests/conftest.py`

### Commands
- Quick (relevant subset): `cd libs/core && pytest tests/unit/<test_file>.py -v`
- Full suite: `make test`
- Coverage: `make coverage` (70% minimum)

### When to run
- After behavior change: run relevant test subset
- Before PR: run full suite (`make test`)
- After refactor: run full suite

### Bugfix rule
1. Add failing test first
2. Confirm failure
3. Implement fix
4. Rerun tests until green

## Doc Update Mapping

| Change Type | Update Location |
|-------------|-----------------|
| Feature logic/inputs/outputs | `docs/features/<feature>.md` |
| User journeys | `docs/app-workflows.md` |
| System layout/integrations | `ARCHITECTURE.md` |
| Dev/testing process | `docs/dev-workflows.md` |
| Setup or tech stack | `README.md` |
| Active work | `docs/exec-plans/active/` |
| Known tech debt | `docs/exec-plans/tech-debt-tracker.md` |
