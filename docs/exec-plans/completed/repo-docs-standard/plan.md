---
type: Plan
title: Adopt the Repo Docs Standard (OKF frontmatter, docs lint, review gates)
description: Bring genblaze's docs, review gates, and agent entry files in line with the org Repo Docs Standard, warning on untouched legacy docs.
status: stable
stale_after: 2027-02-16T00:00:00Z
owner: "@jeronimodeleon"
tags: [docs, ci, agents]
---

# Adopt the Repo Docs Standard

**Status:** completed. Shipped in #347. The base-branch judging acceptance
test passed on throwaway PR #349. Not pursued: the hashed Jira-key and host
lists stay empty, so the leak guard only checks Atlassian links. Left to the
maintainer as GitHub settings: branch protection (code-owner review,
dismiss stale approvals).

## Goal

genblaze is a public OSS monorepo (18 packages). Most of the standard already
exists here: AGENTS.md, ARCHITECTURE.md, SECURITY.md, CODEOWNERS, a PR template,
`docs/index.md`, exec-plans and `/verify-docs`. This plan closes the remaining
gaps without renaming folders or rewriting existing docs.

## Gap analysis

| Standard item | genblaze today | Action |
|---|---|---|
| `CLAUDE.md` imports `@AGENTS.md` | Says "follow AGENTS.md" in prose | Replace with `@AGENTS.md` plus the Claude-only lines |
| AGENTS.md: commands, boundaries, done-checks | No single "done" command, no `.internal/` line | Add `make verify`, generated-file boundaries and a conditional `.internal/AGENTS.md` pointer |
| CODEOWNERS covers the gate files | Covers `.github/`, libs and cli | Add AGENTS.md, CLAUDE.md, REVIEW.md, `.claude/` and `.mcp.json` |
| PR template: Testing, Risk, How to revert, semver | Summary, Changes, and a checkbox test plan | Rewrite the sections |
| One verify command shared by CI and agents | `make typecheck` covers core only; CI typechecks 16 connectors inline. No end-to-end user-flow check (`release-smoke` only imports wheels) | Add `make verify` including an E2E smoke; align `typecheck` with CI and have CI call the make target; `.claude/skills/verify/` wraps it |
| REVIEW.md as the tuned single reviewer | Missing | Severity tags, diff-triggered reliability checklist seeded from past incidents |
| OKF frontmatter in `docs/` | `<!-- last_verified -->` comment in 66 files | Required on new or edited `docs/**` files; `stale_after` replaces the comment when a doc is touched |
| Docs lint in CI | Manual `/verify-docs` skill | `tools/check_docs.py` plus a `docs` workflow. Blocks on changed docs and added lines; untouched legacy docs only warn |
| Weekly freshness check | None | Non-blocking step on `security.yml`'s existing Monday cron |
| Lifecycle files | Flat `exec-plans/active/*.md` | New work uses `<feature>/plan.md`; existing flat plans stay where they are |
| OSS internal-leak guard | None | Hashed-pattern scan of added lines, PR title and body, and commit messages |
| `.internal/` companion repo | Only `CLAUDE.local.md` is ignored | Ignore `.internal/`; add `.worktreeinclude` |

**Skipped, with reasons:**
- `evals/`: no code in this repo grades model output.
- Per-package AGENTS.md: no package's rules differ from the root.
- `product-specs/` and `design-docs/`: created with the first feature that needs them. Empty indexes go stale.
- Commit verification stamps and an observe/enforce commit gate: deferred until `make verify` has run reliably for a few weeks (agents here don't commit anyway).
- Evals that grade the checks themselves (recall on past reverts/incidents): deferred; seed data is collected now via REVIEW.md's incident links.
- `.docs-lint.yml`: the constants live in the script until the org's reusable action exists.

## Decisions

1. **Frontmatter scope is `docs/**` only.** Root files and package READMEs are
   excluded. CLAUDE.md stays an import shim, and the READMEs ship to PyPI.
2. **Type mapping.**
   - `features/`, `guides/` and `reference/` docs use `Library`.
   - Plans use `Plan`.
   - `reference/model-matrix.md` is generated and exempt.
   - `batch-plan-*.md` and `maintenance-report-*.md` are exempt by path.
3. **One freshness rule.** When a doc has `stale_after`, that date decides
   freshness and the 60-day `last_verified` rule no longer applies to it.
   `/verify-docs` calls `check_docs.py` for links and stamps, and keeps only the
   checks that need judgment: owning-module drift, `ast.parse` on examples and
   canonical files.
4. **`make verify` = `lint` + `typecheck` (core and connectors) + `test` + `docs-lint` + `e2e-smoke`.**
   CI's typecheck job calls `make typecheck` and a CI step runs `make e2e-smoke`.
   `ts-types-check` stays CI-only because it needs Node. There is no live-B2
   step: tests are mocked.
   - `tools/e2e_smoke.py` exercises the real user flow, not units: build a
     `Pipeline` with the public `genblaze_core.mocks` provider, run it, save a
     PNG with an embedded manifest, then shell out to the installed
     `genblaze extract` and `genblaze verify` CLI and assert exit 0 and a
     matching `canonical_hash`. Must stay under ~10 s and deterministic.
   - `.claude/skills/verify/SKILL.md` wraps `make verify`: run it before
     opening a PR, paste the output into the PR's Testing section.
5. **Diff-scoped enforcement.** A new `docs.yml` workflow:
   - runs with `fetch-depth: 0` on `pull_request` types `[opened, edited, synchronize, reopened]`
   - has `contents: read` permissions and the Dependabot `if:` guard
   - passes the PR title and body through `env:` only
   - on `push: main`, runs the whole-tree checks in warn mode
   - treats renames as modified files and skips deleted ones
6. **The leak guard stores SHA-256 hashes**, not plaintext, of our Jira project
   keys and internal hostnames, so the public repo doesn't publish the list. The
   guard tokenizes candidates (`\b[A-Z][A-Z0-9]+-\d+\b` and URL hosts) and
   compares hashes. This needs no secrets, so it works on fork PRs.
   The Atlassian Cloud domain is public and is matched in plaintext.
7. **PyYAML is declared in core `[dev]`** because `tools/tests` runs in CI.
8. **A change can't edit the check that judges it.** `docs.yml` checks out
   the PR's base SHA into a separate directory and runs *that* copy of
   `check_docs.py` (and its hash list) against the PR tree. A PR that edits
   the lint is judged by the old lint; the new one takes effect after merge.
   Code Review reads REVIEW.md from the default branch already.
9. **REVIEW.md content.**
    - Severity: Important / Nit / Pre-existing; nits capped at 3; every
      finding cites `file:line`; escalations say why.
    - A diff-triggered reliability checklist: each item has a trigger readable
      from the diff, the check, and the incident it came from. Seed it from
      real history: canonical JSON key order/floats, identity fields as path
      components (#328, #284), `EmbedPolicy` on every embed path, tokens
      never in manifests, provider `submit/poll/fetch_output` contract, wire
      model change ⇒ `make ts-types`. Reviewer checks only matching items.
    - Skip paths: `libs/spec/ts/`, `docs/reference/model-matrix.md`, batch docs.
    - Every revert or incident adds a test, a lint or a checklist item; prune
      items that haven't fired in a year.
10. **Agent rules in AGENTS.md.**
    - Run the change, not just the tests: `make verify` includes the E2E smoke.
    - If a check still fails after three fix attempts, stop and ask a human.
    - Flaky tests: never skip, add sleeps or retry-until-green. Find the root
      cause, or delete the test and record why in the PR.
    - Prefer Hypothesis property tests for round-trips and invariants
      (canonical JSON, embed/extract), following `test_canonical_properties.py`.
11. **Agent pointers move to the folder form.** CLAUDE.md and
   `genblaze-maintainer.md:173` point to `<feature>/plan.md` plus frontmatter.
   Batch tooling paths are unchanged.

## Files

- **Edit:**
  - entry files: `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`
  - GitHub config: `.github/CODEOWNERS`, `.github/pull_request_template.md`, `.github/workflows/ci.yml`, `.github/workflows/security.yml`
  - build: `.gitignore`, `Makefile`, `libs/core/pyproject.toml` (dev extras)
  - docs: `docs/index.md`, `docs/dev-workflows.md`
  - agents: `.claude/skills/verify-docs/SKILL.md`, `.claude/agents/genblaze-maintainer.md`
- **New:**
  - `REVIEW.md`, `.worktreeinclude`
  - `tools/check_docs.py`, `tools/tests/test_check_docs.py`
  - `tools/e2e_smoke.py`, `.claude/skills/verify/SKILL.md`
  - `.github/workflows/docs.yml`

## Test plan (TDD, `tools/tests/test_check_docs.py`)

- **Required files:** a missing `AGENTS.md`, `CLAUDE.md` or `docs/index.md` is an error.
- **Frontmatter:**
  - A changed doc missing `type`, `owner`, `status` or `stale_after` is an error. An unchanged doc gets a warning.
  - `index.md` and `log.md` are exempt in any folder.
  - A `type` or `status` outside the controlled list is an error.
  - Error cases:
    - malformed YAML, reported as an error rather than a crash
    - unquoted `owner: @x`
    - a `---` rule at line 1 with no closing fence
  - BOM and CRLF are accepted.
  - The path exemptions are honoured.
- **Timestamps:**
  - `stale_after` parsed as a `datetime` with a `Z` offset passes.
  - A date-only or offset-less value is an error.
  - A past date is a warning only.
- **Links:**
  - A relative link to a missing file is an error.
  - External URLs and `#anchors` are skipped.
  - Links inside fenced code are ignored.
- **Commands:** `make <target>` in AGENTS.md with no Makefile target is an error. `/skills` and `pytest` invocations are not checked.
- **Leak guard:**
  - A hashed key in an added line, the PR title or a commit message is an error.
  - These are not matched: `UTF-8`, `SHA-256`, `CVE-2026-1234`, `GHSA-xxxx-…`, `PEP-621`, and the key's prefix inside a longer word.
- **Exit codes:** warn-only mode exits 0 with warnings. Errors exit 1. Output uses GitHub `::warning`/`::error file=…,line=…` annotations.
- **Base-branch judging:** a PR that edits `check_docs.py` to drop a rule still
  fails on that rule in `docs.yml`. Verified after merge on throwaway PR #349.
- **Links:** a broken link on an added line is an error; an older broken link
  in an edited file only warns, so a CHANGELOG entry doesn't inherit it.
- **E2E smoke:** fails (non-zero) if the embedded manifest is tampered with
  after save, if `genblaze verify` is missing from PATH, or if the hash
  doesn't match; passes three runs in a row (flake check).
- **Acceptance (not unit tests):**
  - `make verify` is green locally.
  - The `docs.yml` run on this PR is green.
  - `/verify-docs` still produces its report.

## Risks and accepted gaps

- **CODEOWNERS has one owner**, so the "security owner" rule can't be met yet. The gate does nothing until branch protection requires code-owner review, which is a GitHub setting the maintainer has to turn on.
- **Stale approvals:** "a passing review on an out-of-date commit means
  nothing." Turn on "dismiss stale approvals on new commits" in branch
  protection (maintainer, GitHub setting).
- **Hashed patterns can't be audited by reading the file.** The maintainer keeps the plaintext list in the `.internal/` companion repo.
- **`.worktreeinclude` copies files rather than linking them**, so it lists only `CLAUDE.local.md`. `.internal/` is symlinked per worktree, as documented in CONTRIBUTING.md, so each worktree doesn't get a nested git clone.
- **Ruff version drift:** `make lint` uses whatever ruff is installed, while CI pins 0.14.0. Pin the same version in core `[dev]`.
