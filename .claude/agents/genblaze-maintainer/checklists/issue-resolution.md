# Issue Resolution Playbook

Use this when invoked to resolve a GitHub issue end-to-end. The goal is a
**production-ready PR that is ready to merge** — then stop for human review.
Never merge or self-approve.

Mark `[x]` when done, `[!]` when blocked.

## 1. Triage (read-only)
- [ ] `gh issue view <N> --comments` — read the issue and all discussion
- [ ] Classify: **bug**, **feature**, **docs**, or **non-code**
- [ ] **Scope guard** — if the issue is a question, `needs-info`, `wontfix`, a
      duplicate, or otherwise should not produce code, post a helpful
      `gh issue comment <N>` explaining next steps and **stop**. Do not write code.
- [ ] Reproduce the problem (failing command, script, or test) and capture it
- [ ] **DRY / dedup check** before writing anything:
  - `gh pr list --search "<N> in:title,body"` and `gh pr list --head <branch>` —
        is there already an open PR for this issue? If so, resume it, don't dup.
  - `git branch -a --list "*issue-<N>*"` — does a branch already exist?
  - Search the codebase for existing helpers/patterns that already solve it
  - Check `docs/exec-plans/tech-debt-tracker.md` — is this a known item?

## 2. Plan
- [ ] For a **single-file or small fix**: no planning doc. Proceed.
- [ ] For a **multi-file change or new feature**: write a concise exec-plan in
      `docs/exec-plans/active/` (goal, files, decisions, risks), then red-team it
      before coding (per the engineering working agreement).

## 3. Branch (off updated main, not the worktree HEAD)
This agent runs in an isolated worktree whose HEAD may be stale, so branch
explicitly from the remote default branch:
```bash
git fetch origin
git switch -c <type>/issue-<N>-<slug> origin/main   # type: fix | feat | docs | refactor
```
- [ ] Branch name follows the repo convention (`fix/issue-70-resume-post-submit-retry`)

## 4. Implement (TDD, minimal, idiomatic)
- [ ] **Write the failing test first** (see CONTRIBUTING.md test-placement table:
      core unit → `libs/core/tests/unit/`, golden → `libs/core/tests/golden/`,
      CLI → `cli/tests/`, shared fixtures → `libs/core/tests/conftest.py`)
- [ ] Implement the **smallest** change that makes it pass
- [ ] Match the surrounding code's style and patterns — do not refactor for
      style alone. **No performance work unless the issue is about performance.**
- [ ] Handle edge cases the issue implies (empty/None inputs, error paths,
      concurrency if the touched code is async)
- [ ] Respect every invariant in `AGENTS.md` (canonical-hash determinism,
      `submit/poll/fetch_output`, UUIDs, `EmbedPolicy`, Pydantic v2, no tokens
      in manifests)
- [ ] Public functions get docstrings; errors say what failed + what to try next

## 5. Verify (must be green before the PR)
- [ ] `make test` — full suite passes (or `/test-package <pkg>` while iterating,
      then `make test` before the PR)
- [ ] `make lint` — clean
- [ ] `make typecheck` — review and resolve errors
- [ ] `make coverage` — stays at/above the 70% gate
- [ ] If you changed a Pydantic model in `libs/core/genblaze_core/models/`:
      update `libs/spec/schemas/manifest/v1/`, run `make ts-types`, and
      **commit the regenerated `libs/spec/ts/genblaze.d.ts`** — CI's `ts-types`
      job diffs it and will fail the PR otherwise
- [ ] If you touched `examples/`, confirm they still compile

## 6. Docs + changelog (same PR as code)
- [ ] Update affected docs (`README.md`, `docs/features/*.md`, per-package
      READMEs) in this PR — behavior changes without doc updates fail review
- [ ] Add a `[Unreleased]` bullet to `CHANGELOG.md` **under the correct package
      heading** (map the files you changed → their package). The release
      `changelog-gate` depends on this.
- [ ] If you wrote an exec-plan, move it to `completed/` when the work lands

## 7. Commit
- [ ] Conventional Commit messages (`fix(core): ...`, `feat(cli): ...`), imperative,
      ≤72-char subject, body explains *why*
- [ ] One logical change per commit; never force-push

## 8. Pre-PR triangulated review (before any push)
Spawn **three independent `Agent` sub-agents** to review the committed branch
(`git diff origin/main...HEAD`). Give each only its own lens — they must not see
each other's findings, so their agreement is real triangulation, not groupthink.
- [ ] **Reviewer A — Correctness & tests**: does it actually fix issue `#<N>`?
      Edge cases, regressions, test quality (no `assert True`, real assertions).
- [ ] **Reviewer B — Security & invariants**: secrets, injection, SSRF, unsafe
      (de)serialization; every `AGENTS.md` invariant (canonical-hash determinism,
      `EmbedPolicy`, no tokens in manifests, UUIDs, Pydantic v2).
- [ ] **Reviewer C — Architecture, scalability & DRY**: fits existing patterns,
      no duplicated/parallel logic, no needless complexity, holds up at load.
- [ ] Each reviewer returns findings tagged **P0/P1/P2**
- [ ] **Triangulate**: any **P0**, or any issue flagged by **≥2 reviewers**, is
      **blocking**. Fix it, re-run section 5 (verify), and re-review until no
      blocking findings remain.
- [ ] Capture the three verdicts to paste into the PR body

## 9. Open the PR — then STOP
```bash
git push -u origin <branch>
gh pr create --fill-first \
  --title "<conventional-commit subject>" \
  --body "<filled .github/pull_request_template.md, with 'Closes #<N>'>"
```
- [ ] PR body follows `.github/pull_request_template.md` (Summary / Changes /
      Test plan / Related) and includes **`Closes #<N>`** so the issue auto-closes
- [ ] Check the Test plan boxes you actually verified
- [ ] Include the triangulated-review summary (the three reviewer verdicts) so
      the human reviewer sees what was already checked
- [ ] Reviewers and labels are **best-effort**: try `gh pr edit --add-reviewer`
      / `--add-label`; if they fail (solo repo, unknown handle), continue — do
      **not** abort the PR
- [ ] **Stop here.** Report the PR URL. Do not merge, do not enable auto-merge,
      do not approve.

## Failure path (never leave it hanging)
If you cannot get to green (`make test`/`lint`/`typecheck` keep failing, or the
fix needs a decision you can't make):
- [ ] Push the work-in-progress branch
- [ ] Open a **draft** PR (`gh pr create --draft`) describing what's done, what's
      blocking, and the failing output — or, if nothing is committable, report
      back with the blocker. Either way, surface it clearly; do not stall silently.
