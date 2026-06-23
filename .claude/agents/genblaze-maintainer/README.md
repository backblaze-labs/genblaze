# Genblaze Maintainer

The **Genblaze Maintainer** is an autonomous Claude Code sub-agent that serves as the
dedicated guardian of the Genblaze repository. It runs in one of two modes:

- **Issue Resolution** — takes a GitHub issue from triage to a **merge-ready PR**:
  branches off `origin/main`, fixes it with TDD, verifies, runs a triangulated
  three-reviewer check, opens the PR, then **stops for human review**.
- **Maintenance Audit** — audits the codebase across six domains (functional
  integrity, security, code quality, documentation, AI agent standards, and
  dependency health) and produces a structured report with prioritized findings.

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
    run.sh                      # CLI launcher script
    config.json                 # Agent configuration
    checklists/
      issue-resolution.md       # Issue → merge-ready PR playbook (with triangulated review)
      functional.md             # Build, test, import validation
      security.md               # Security audit checklist
      code-quality.md           # Lint, types, patterns
      documentation.md          # Doc accuracy and completeness
      agent-standards.md        # AI agent optimization
      dependencies.md           # Supply chain health
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
