# Genblaze Maintainer

The **Genblaze Maintainer** is an autonomous Claude Code sub-agent that serves as the
dedicated guardian of the Genblaze repository. It runs in one of these modes:

- **Issue Resolution** — takes a GitHub issue (or a **cluster** of related
  issues) from triage to a **merge-ready PR**: branches off `origin/main`, fixes
  it with TDD, verifies, runs a triangulated three-reviewer check, opens the PR,
  then **stops for human review**.
- **Maintenance Audit** — audits the codebase across six domains (functional
  integrity, security, code quality, documentation, AI agent standards, and
  dependency health) and produces a structured report with prioritized findings.
- **Batch** — reviews **all** open issues at once, clusters and dependency-orders
  them, and (after human approval) resolves each cluster into a PR. Driven by two
  workflows with a hard approval gate between them; see **Batch mode** below.

## Quick Start

**Recommended — invoke directly in Claude Code:**
```
@genblaze-maintainer resolve issue #70
@genblaze-maintainer fix the bug in https://github.com/backblaze-labs/genblaze/issues/77
@genblaze-maintainer run a full maintenance audit
@genblaze-maintainer audit the security domain only
@genblaze-maintainer check documentation accuracy
```

**Or via the launcher script:**
```bash
# From the genblaze repo root:
.claude/agents/genblaze-maintainer/run.sh --issue 70         # Resolve issue #70 → PR
.claude/agents/genblaze-maintainer/run.sh                    # Full audit
.claude/agents/genblaze-maintainer/run.sh --domain security  # Security only
.claude/agents/genblaze-maintainer/run.sh --fix              # Auto-fix P0/P1
.claude/agents/genblaze-maintainer/run.sh --report-only      # Read-only
.claude/agents/genblaze-maintainer/run.sh --model opus       # Use Opus
```

## How It Works

### Issue Resolution mode

Triage → branch off `origin/main` → TDD fix (minimal, idiomatic) → verify
(`make test`/`lint`/`typecheck`/`coverage`, plus `make ts-types` when a core
schema changes) → docs + `CHANGELOG` `[Unreleased]` → commit → **pre-PR
triangulated review** (three independent `Agent` sub-agents — correctness,
security, architecture — whose findings must converge; a P0 or any issue raised
by 2+ reviewers blocks the PR) → push and open a merge-ready PR with `Closes #N`.
It **stops there** — it never merges, auto-merges, or self-approves. If it can't
reach green it opens a **draft** PR describing the blocker instead of stalling.
Full playbook: [`checklists/issue-resolution.md`](checklists/issue-resolution.md).

### Maintenance Audit mode

The agent follows a four-phase execution protocol:

1. **Discovery** — Reads docs, runs tests/lint/typecheck, scans for issues
2. **Assessment** — Categorizes findings by severity (P0/P1/P2) across six domains
3. **Remediation** — Fixes issues (if authorized), one commit per fix, tests after each
4. **Reporting** — Writes a structured report to `docs/exec-plans/active/`

### Batch mode

Batch mode puts the maintainer in charge of the **whole open-issue list** while
keeping a human firmly in control. It is deliberately split into two workflows
with a hard approval gate between them, because a workflow cannot pause for human
input mid-run — so the gate becomes an explicit, auditable boundary.

```
# 1. Plan — review all issues, cluster + order them, write an approval-gated doc, STOP.
/batch-plan                       # (or: Workflow tool, name: "batch-plan")

# → review docs/exec-plans/active/batch-plan-<date>.md, then:

# 2. Execute — validate the approved plan against live state, open one PR per cluster.
/batch-execute {"docPath": "docs/exec-plans/active/batch-plan-<date>.md", "dryRun": true}
/batch-execute {"docPath": "docs/exec-plans/active/batch-plan-<date>.md"}
```

**Safety model:**

- **Deterministic core, tested.** Clustering, plan-doc integrity, and all git
  hygiene live in `tools/batch_*.py` under `make test` — not in prose. Agents
  only do the one fuzzy step (estimating which files an issue touches) and act as
  pipes to those CLIs.
- **Preflight never touches your checkout.** It fails fast on a dirty tree, and
  fast-forwards local `main` only when `main` is not checked out and strictly
  behind — never switching, stashing, or committing.
- **The gate is enforced, not assumed.** The plan doc embeds the `origin/main`
  SHA and a content token; `batch-execute` re-validates against live state and
  aborts if main advanced, the doc was hand-edited, or a planned issue closed.
- **Conflict-aware, capped clusters.** Issues sharing real (non-"hot") files
  cluster into one PR up to a size cap; oversized or dependent work is deferred to
  a re-plan rather than stacked (stacked branches break when a prerequisite is
  squash-merged). Each cluster still goes through the full per-PR bar and opens a
  **draft** PR if it hits a real conflict.
- **Conservative cleanup.** `gc` prunes only merged, pushed leftover branches
  with `git branch -d` (never `-D`), skips dirty worktrees, and reports anything
  it retains. Nothing you haven't shipped is ever destroyed.

## Domains

| Domain | Priority | What It Checks |
|--------|----------|---------------|
| Security | P1 | Secrets, injection, CVEs, network safety |
| Functional | P2 | Tests, imports, examples, CLI commands |
| Code Quality | P3 | Lint, types, patterns, dead code |
| Documentation | P4 | Accuracy, completeness, cross-refs |
| Agent Standards | P5 | Type hints, exports, error messages |
| Dependencies | P6 | Versions, optional deps, supply chain |

## File Structure

```
.claude/agents/
  genblaze-maintainer.md        # Agent definition (frontmatter + prompt)
  genblaze-maintainer/
    README.md                   # This file
    run.sh                      # CLI launcher script (single-issue / audit)
    config.json                 # Agent configuration
    checklists/
      issue-resolution.md       # Issue → merge-ready PR playbook (with triangulated review)
      functional.md             # Build, test, import validation
      security.md               # Security audit checklist
      code-quality.md           # Lint, types, patterns
      documentation.md          # Doc accuracy and completeness
      agent-standards.md        # AI agent optimization
      dependencies.md           # Supply chain health
.claude/workflows/
  batch-plan.js                 # Batch mode: review all issues → approval-gated plan doc
  batch-execute.js              # Batch mode: validate plan → one PR per cluster
tools/
  batch_cluster.py              # Deterministic clustering (tested)
  batch_plandoc.py              # Plan-doc write + drift-proof validation (tested)
  batch_gitsteward.py           # Safe preflight + conservative gc (tested)
```

## Permissions

Issue Resolution mode performs outward git/GitHub actions, so the running
environment must allow them or the agent will pause for approval at each step.
Add these to `.claude/settings.json` (or your `settings.local.json`) if they
aren't already permitted:

```jsonc
"Bash(git fetch:*)", "Bash(git switch:*)", "Bash(git add:*)",
"Bash(git commit:*)", "Bash(git push:*)",
"Bash(gh issue view:*)", "Bash(gh issue comment:*)",
"Bash(gh pr create:*)", "Bash(gh pr edit:*)", "Bash(gh pr ready:*)"
```

Safety boundaries are intentional and should stay: `git push --force` is denied,
and the agent never merges, auto-merges, or self-approves — a human owns the
merge. Granting `git commit`/`git push` here auto-approves them repo-wide, so add
them only if you want this agent (and other sessions) to push without prompting.

## Reports

Reports are written to `docs/exec-plans/active/maintenance-report-{date}.md` and
follow a structured format with findings categorized by severity, actions taken,
test results, and recommended next steps.

## Invariants

The Genblaze Maintainer enforces all invariants from `AGENTS.md`:

- All changes must pass `make test`
- Canonical JSON hashing remains deterministic
- Manifest hashes always verify
- Providers implement submit/poll/fetch_output
- All IDs are UUIDs
- EmbedPolicy respected everywhere
- Pydantic v2 only
- Docs updated with code
- Python 3.11+
- No API tokens in manifests
