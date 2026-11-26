<!-- last_verified: 2026-11-16 -->
# Agents

Orchestration framework for generative media pipelines with manifest-based
provenance. 21 pip packages: `genblaze-core` (`libs/core/`), 18 connectors
(`libs/connectors/*`), `genblaze-cli` (`cli/`), and the `genblaze` umbrella
(`libs/meta/`). Read order: README.md → ARCHITECTURE.md → this file → the
relevant `docs/features/` doc. Contributor process: [CONTRIBUTING.md](CONTRIBUTING.md).

**Internal context:** if `.internal/AGENTS.md` exists, read it too. If it
doesn't, use public docs only. Nothing from `.internal/` (ticket keys,
internal links, specs) may appear in tracked files, branch names, PR titles,
PR bodies or commit messages. The docs lint flags Atlassian links today; its
hashed key and host lists are still empty, so check by hand.

## Commands

- Setup: `make install-dev`
- **Done check: `make verify`.** Runs lint, typecheck (core + connectors),
  every test suite, the docs lint and the end-to-end CLI smoke. CI runs the
  same targets. Run it before opening a PR and paste its output into the PR's
  Testing section (`/verify` does both).
- Iterate: `cd libs/core && pytest tests/unit/<file>.py -v`, or
  `/test-package <name|changed>`
- Pieces of verify: `make lint`, `make typecheck`, `make test`,
  `make docs-lint`, `make e2e-smoke`
- Format: `make fmt`
- Wire-model change: `make ts-types` (needs Node), then commit `libs/spec/ts/`
- New connector: `/scaffold-provider`. Releases: `make pre-release` and
  [RELEASING.md](RELEASING.md); after publish `make post-release VERSION=<umbrella>`.

## Architecture boundaries

- `libs/core/`: core SDK; no provider API dependencies
- `libs/connectors/<name>/`: one provider each; depends on core only
- `cli/`: depends on core
- `libs/spec/`: language-neutral wire contract. JSON Schemas are
  authoritative; a Pydantic model change must update the schema, run
  `make ts-types`, and pass `test_spec_conformance.py`.
- Full detail: [ARCHITECTURE.md](ARCHITECTURE.md)

## Invariants

- Canonical JSON hashing stays deterministic: never change key sort order or
  float normalization. `canonical_hash` must verify after re-serialization.
- Provider tokens never land in manifests, embedded media or logs.
- Providers implement `submit` / `poll` / `fetch_output`, no exceptions.
- Identity fields (`run_id`, `step_id`, `asset_id`, `tenant_id`,
  `parent_run_id`) are untrusted path components: validate before joining
  into a filesystem path or object key.
- `EmbedPolicy` is respected on every embedding path.
- All IDs are UUIDs. Pydantic v2 only. Python 3.11+.

## Boundaries

- Generated, never hand-edit: `libs/spec/ts/genblaze.d.ts` (`make ts-types`),
  `docs/reference/model-matrix.md` (`tools/gen_model_matrix.py`).
- Needs a maintainer's approval: changes to AGENTS.md, CLAUDE.md, REVIEW.md,
  `.claude/`, `.github/` and release tooling (CODEOWNERS); version bumps and
  CHANGELOG wave cuts (`/prepare-release`); anything touching auth, tokens or
  storage credentials.
- A change must not edit the check that judges it: don't modify
  `tools/check_docs.py`, `tools/e2e_smoke.py` or REVIEW.md to make your own
  change pass. CI runs the base branch's copy of `check_docs.py`; the e2e
  smoke and REVIEW.md rely on CODEOWNERS review instead.
- CI policy: Dependabot PRs run no CI. Every job in a `pull_request`-triggered
  workflow carries `if: ${{ github.actor != 'dependabot[bot]' &&
  github.event.pull_request.user.login != 'dependabot[bot]' }}`; copy it onto
  any new job. `release.yml` has no `pull_request` trigger and needs none.

## Verification rules

- Run the change, not just the unit tests: `make verify` includes an
  end-to-end pipeline → embed → `genblaze extract`/`verify` smoke.
- If a check still fails after three fix attempts, stop and ask a human.
- Flaky tests: never skip, add sleeps or retry until green. Find the root
  cause, or delete the test and record why in the PR.
- Prefer Hypothesis property tests for round-trips and invariants (see
  `libs/core/tests/unit/test_canonical_properties.py`).
- Every revert or incident adds a test, a lint, or a REVIEW.md checklist item.

## Docs and plans

- Docs change in the same PR as the code ([Doc Update Mapping](docs/dev-workflows.md#doc-update-mapping)).
- Every new or edited `docs/**/*.md` starts with OKF frontmatter: `type`
  (Service|Library|Intent|Spec|Plan|Decision), `title`, `description`,
  `status` (draft|stable|deprecated), `stale_after` (ISO 8601 UTC), `owner`.
  `index.md` and `log.md` are exempt. `make docs-lint` enforces it.
- Plans for multi-file changes, features and refactors:
  `docs/exec-plans/active/<feature>/plan.md` (`type: Plan`); move the folder
  to `completed/` when its last task merges. Bug fixes that keep behavior need
  no plan; the PR's Testing section is the record.
- Outside contributors link a GitHub issue instead of writing plan files.

## Doc map

- [docs/index.md](docs/index.md): every doc with a one-line description
- [docs/dev-workflows.md](docs/dev-workflows.md): engineering workflows
- [docs/features/maintainer-agent.md](docs/features/maintainer-agent.md): maintainer agent flow
- [docs/exec-plans/active/](docs/exec-plans/active/) and [tech-debt-tracker.md](docs/exec-plans/tech-debt-tracker.md)
- [REVIEW.md](REVIEW.md): what code review flags here
- [SECURITY.md](SECURITY.md): vulnerability reporting
