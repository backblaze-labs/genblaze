# Genblaze Maintainer

The **Genblaze Maintainer** is an autonomous Claude Code sub-agent that serves as the
dedicated guardian of the Genblaze repository. It audits the codebase across six domains
— functional integrity, security, code quality, documentation, AI agent standards, and
dependency health — then produces a structured maintenance report with prioritized findings.

## Quick Start

**Recommended — invoke directly in Claude Code:**
```
@genblaze-maintainer run a full maintenance audit
@genblaze-maintainer audit the security domain only
@genblaze-maintainer check documentation accuracy
```

**Or via the launcher script:**
```bash
# From the genblaze repo root:
.claude/agents/genblaze-maintainer/run.sh                    # Full audit
.claude/agents/genblaze-maintainer/run.sh --domain security  # Security only
.claude/agents/genblaze-maintainer/run.sh --fix              # Auto-fix P0/P1
.claude/agents/genblaze-maintainer/run.sh --report-only      # Read-only
.claude/agents/genblaze-maintainer/run.sh --model opus       # Use Opus
```

## How It Works

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
      functional.md             # Build, test, import validation
      security.md               # Security audit checklist
      code-quality.md           # Lint, types, patterns
      documentation.md          # Doc accuracy and completeness
      agent-standards.md        # AI agent optimization
      dependencies.md           # Supply chain health
```

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
