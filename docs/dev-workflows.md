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

## Catalog drift detection

Provider catalogs change faster than the SDK ships. Two probe tools live in `tools/`; both make real network calls against live APIs and write reports to `docs/reference/`.

| Tool | Scope | Report path |
|------|-------|-------------|
| `tools/probe_models.py` | Every entry-point-registered provider's `example_slugs`. Exits 1 on any `NOT_FOUND`. | `docs/reference/model-probe-status.json` |
| `tools/probe_gmicloud_wire.py` | GMICloud-specific: per-slug casing, per-i2v image wire-key, PixVerse duration coercer. | `docs/reference/gmicloud-wire-probe-{date}.{json,md}` |

### When to run

- Before tagging a release wave — catches model rotations that would silently break quickstarts.
- When a user reports a 404 on a slug listed in the README or `example_slugs`.
- On a weekly schedule via `.github/workflows/probe-catalog.yml` (auto-opens / comments on a `[catalog-drift]` issue when any provider returns `NOT_FOUND`).

### Onboarding a new provider's probe coverage

1. Add the `GENBLAZE_PROBE_<NAME>_API_KEY` mapping to the workflow's `env:` block, pointing at the appropriate staging secret. Multiple entry points sharing one upstream account (e.g. `nvidia-video`/`nvidia-image`/`nvidia-audio`/`nvidia-chat`) reuse the same `<provider>_API_KEY_STAGING` secret.
2. Configure that secret in repo Settings → Secrets and variables → Actions.

That's it — no further code or CI changes. `probe_models.py` walks every entry-point-registered provider and skips those without credentials, so unconfigured providers stay quiet until their secret comes online.

### Isolation guarantees (missing-key behavior)

The probe is designed so any one provider's missing credential, broken package, or upstream outage cannot block coverage for the others:

| Failure | Behavior |
|---------|----------|
| Provider's GH secret not configured | Env var resolves to empty → `probe_models.py` marks that provider `skipped` → no failure, no issue |
| Provider's package fails to install | Workflow's tolerant install loop captures the error as a `::warning::` and continues; `probe_models.py` doesn't see that provider in entry points and silently skips it |
| Provider's upstream API down (transient 5xx) | Probe records `unknown` status; does **not** trigger a `[catalog-drift]` issue (only `not_found` does) |
| GMI staging key absent | `probe_gmicloud_wire.py` step is gated on `env.GMI_API_KEY != ''` and skipped cleanly; other probes unaffected |

The only outcome that opens an issue is a configured provider returning `not_found` — i.e. real catalog drift on a provider the workflow can actually exercise.

### Local invocation

```bash
# All providers (skips any whose GENBLAZE_PROBE_<NAME>_API_KEY isn't set):
python tools/probe_models.py

# Just the GMI wire-conformance probe (slug casing + wire keys):
export GMI_API_KEY="gmi-..."
python tools/probe_gmicloud_wire.py
```

### Reading the report

- **`ok`** — slug accepted by upstream.
- **`not_found`** — upstream returned 404. The slug is dead; prune it from family `example_slugs` and any README quickstart.
- **`auth`** — credential rejected. Re-check the probe env var.
- **`skipped`** — no credential supplied; not a failure.
- **`unknown`** — transient (5xx, network blip). Re-run; do NOT prune.

Cross-reference the wire-probe's slug-case matrix before pruning a "not_found" lowercase slug — GMICloud may accept the PascalCase variant of the same model.

### Audit-log cost

Each `probe_gmicloud_wire.py` run creates ~25 audit-log entries on the configured GMI account (matrix submissions, each rejected by upstream as expected). `probe_models.py` adds ~one entry per registered slug. Run weekly maximum; do not gate per-PR CI on these probes.

### Discipline rule for README + example_slugs

Every slug used in any README quickstart, provider docstring, or `examples/*_pipeline.py` script **must also appear in its family's `example_slugs` tuple** (or `unstable_examples` for known-flaky slugs). Brings README slugs under the existing probe's coverage without new test infrastructure.

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
