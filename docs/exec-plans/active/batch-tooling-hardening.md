# Batch tooling hardening (`batch-plan` / `batch-execute`)

**Status:** PR 1 merged (#338); PR 2 (approval, ordering) on `feat/batch-approval-and-ordering` · **Owner:** maintainer

## Goal

Make `.claude/workflows/batch-{plan,execute}.js` safe to run on every open issue
(43 today) when issue and comment text is written by untrusted users, and fix
the correctness bugs from the 2026-11-16 panel review. Keep the design:
deterministic, tested Python in `tools/batch_*.py`, thin agents in the JS.

## Threat model

Untrusted input: issue titles, bodies, comments, linked PR text. Assets: the
maintainer's `gh` token (`repo` + `workflow` scopes), PyPI/npm trusted
publishing via `release.yml`, local secrets, `main`. An agent with Bash, Edit
and `pytest` can run arbitrary code (write a test, run `make test`), so
**command filtering is not a boundary**. The boundary is what the agent's
process can reach.

## Design

### 1. Executors are sandboxed and never push

- `.claude/hooks/batch-executor-guard.py` is registered project-wide in
  `.claude/settings.json` (subagent-frontmatter hooks aren't documented to fire
  for Workflow `agent({agentType})` calls) and exits immediately unless hook
  input has `agent_type == "batch-executor"`.
- Stripping credentials from the environment alone is not a boundary: the agent
  controls the command and can `unset GH_CONFIG_DIR; gh ...`. So every executor
  Bash call is re-run under macOS `sandbox-exec` with an emptied environment,
  **no network**, no reads of credential stores (`~/.config/gh`, `~/.ssh`,
  keychains, …), and writes only to the worktree, a per-worktree temp dir and
  the git metadata a commit needs (`objects`, `refs/heads`, the worktree's git
  dir; not `config`, `hooks` or `refs/remotes`). The sandbox is kernel-enforced
  and inherited by child processes, so scripts and tests the agent writes
  can't escape it. Non-macOS: executors are denied.
- File tools: Read/Grep/Glob only inside the worktree (plus
  `.batch/issues`); Edit/Write only inside the worktree and never on a
  `tools/batch_policy.py` path. Any other tool (Agent, WebFetch, …) is denied.
- Executors edit, test and commit locally. They never push, open PRs, comment
  or label, and they skip the maintainer's in-PR review panel (no sub-agents);
  the human reviews each PR.

### 2. A trusted step verifies before anything leaves the machine

New `tools/batch_gitsteward.py publish --cluster <json>`, run by the
orchestrator (not an executor):

1. Re-fetch `origin/main` (the local ref isn't trusted), diff the branch
   against it with `--name-status -M -C` (catches renames) and refuse if any guarded path or dependency file changed, or a
   commit carries a `Co-authored-by` trailer, the diff adds a
   credential-shaped string, or the base is not `origin/main`.
2. Only then `git push` and `gh pr create` with `Closes #N` for every member
   issue taken from the plan (not the agent's report). A blocked branch is
   reported and stays local.

This replaces the post-push "verify" phase and fixes the `r.issues` crash at
`batch-execute.js:160`.

### 3. Untrusted text enters through code; scouts are read-only

- New `tools/batch_issues.py fetch --out <json>`: paginated `gh api` over
  **all** open issues (fails above `--max`, default 200; no silent truncation),
  recording title, body, comments and existing-work signals: open PRs whose
  body says `closes|fixes|resolves #N` (case-insensitive, exact number) and
  exact branch names.
- New `.claude/agents/batch-scout.md` with tools `Read, Grep, Glob` only. Scouts
  get the issue number and the JSON path, never interpolated issue text, and
  are told the issue text is untrusted data.
- The red-team agent writes `<plan>.redteam.md`, never the plan doc.

### 4. Repo-level backstop (needs your OK; not required reviewers)

Set a **deployment-branch policy** on the `pypi` and `npm` environments:
allow only `main` and tags `v*`. A pushed feature branch can then never reach
the publish jobs, even if it edits `release.yml`. One settings change, no
release friction.

### 5. Approval and correctness

- Per-cluster `- [ ] approve <slug>` checklist outside the tokened block.
  Nothing runs unless ticked. `batch_plandoc.py validate` prints the dispatch
  list (approved, executable, no existing PR/branch); the JS uses it verbatim.
- Base-SHA drift becomes a warning (executors branch off live `origin/main`).
- `dryRun` defaults to **true**.
- Clustering: an oversized component's first issue is the lowest-numbered one
  with no unmet dependency inside the component (reproduced: dependent #70 was
  scheduled before prerequisite #75); all-dependent components defer as a
  cycle. `libs/spec/**` stays hot: the generated `genblaze.d.ts` changes with
  most model edits and would merge unrelated issues.
- `validate --issues-dir` reads open issues and existing work from a fresh
  `batch_issues.py` fetch (which now clears stale files), so no agent builds
  that list and re-runs skip clusters that already have a PR or branch.
- Preflight ignores untracked `docs/exec-plans/active/batch-plan-*.md`.
- Executors don't touch CHANGELOG or versions; PR bodies carry a
  `Changelog:` line for `/prepare-release`.

### Cut from the draft (and why)

- Shell command denylist and audit log: bypassable via code the agent writes;
  the credential strip is the real control.
- Content-hash token: bot comments would churn approvals; injection defense
  doesn't depend on it.
- `only`/`skip`/`concurrency` args: the existing-PR/branch skip gives
  idempotent re-runs; the workflow runtime already caps concurrency. Revisit
  after the first real run.

## Files

| File | Change |
|---|---|
| `tools/batch_issues.py` (new) | fetch + existing-work signals |
| `tools/batch_gitsteward.py` | `publish` (verify then push/PR), preflight ignore |
| `tools/batch_cluster.py` | dependency order, spec path |
| `tools/batch_plandoc.py` | approval checklist, dispatch output, SHA warning |
| `.claude/hooks/batch-executor-guard.py` (new) | credential strip + path guard |
| `.claude/agents/batch-scout.md`, `batch-executor.md` (new) | least-privilege agents |
| `.claude/workflows/batch-plan.js`, `batch-execute.js` | wire the above |
| `.claude/agents/genblaze-maintainer/README.md` | usage, cost, safety model |

## Test plan (red → green per behavior)

Extend `tools/tests/test_batch_{cluster,plandoc,gitsteward}.py` (39 tests).

1. Clustering regression first (red): dependent never executable before its
   prerequisite in an oversized component.
2. `publish`: a rename out of `.github/`, a new workflow file, a
   `pyproject.toml`/`uv.lock` change and a co-author trailer each block, and
   `git push` is never called when blocked; a clean branch pushes and the PR
   body lists every plan issue.
3. `test_batch_issues.py`: pagination returns all issues; `--max` overflow
   fails; `#70` doesn't match `#700`; `Fixes`/`resolves` detected.
4. Plan doc: unticked clusters never dispatched; clusters with an open PR
   excluded.
5. Guard hook: rewritten command has no `GH_TOKEN` and an empty
   `GH_CONFIG_DIR`; guarded paths denied; malformed input denied.
6. Integration: in the executor environment, `gh auth status` fails and
   `git push` cannot authenticate.
7. Manual: `dryRun` on a 5-issue slice, then one real cluster.

## Rollout

- PR 1 (security): sections 1–3 plus their tests.
- PR 2 (correctness): section 5.
- Section 4 is a settings change, separate from code.
- Then a supervised dry run, then one real cluster, before a full run.

## Adversarial review (2026-11-17)

An independent attack on the first implementation found, and PR 1 now fixes
with a regression test each:

- `git diff --name-status` quotes non-ASCII or `"` names, so
  `.github/workflows/é.yml` passed `publish` and was pushed. Now parsed with
  `--raw -z`; symlinks and submodules are refused.
- The sandbox could rewrite the worktree's `commondir`/`.git` pointer, making
  the next unsandboxed `git status` run an attacker `core.fsmonitor`. Those
  files are now read-only, and every steward git call passes
  `-c core.fsmonitor= -c core.hooksPath=/dev/null`.
- `refs/heads` and `packed-refs` were writable, so `main` could be moved. Ref
  writes are now limited to `refs/heads/{fix,cluster}/`.
- Credentials outside the original list (`~/.codex`, `~/.b2_account_info`)
  were readable. Reads of `$HOME` are now denied except the repo, the
  toolchains on `PATH`, and git config.
- Case variants (`.Claude/`, `.GitHub/`) passed both layers. Paths are now
  casefolded and NFKC-normalized; `CLAUDE.md`, `AGENTS.md`, `.gitmodules`,
  `.gitattributes`, pre-commit config, `setup.py`/`setup.cfg` and
  `requirements/` are guarded; the trailer match tolerates spacing.
- `osascript`, `open`, `security` and the Apple Events / LaunchServices
  services are blocked.

## Residual risk

- macOS only: `sandbox-exec` is deprecated by Apple but still enforced; if it
  disappears, executors are denied rather than run unsandboxed.
- The sandbox can list (not read) names in the repo's parent directories,
  because git stats them while resolving paths.
- git prints harmless `packed-refs.lock` errors; `packed-refs` stays
  read-only on purpose.
- `find_secrets` catches common token shapes only (not encoded or split
  tokens). The home-read denial is the real control; a human reviews every PR.
- The hook adds ~90 ms (Python startup) to every tool call in every session.
- Re-run the adversarial pass after PR 2 and before the first real batch.
