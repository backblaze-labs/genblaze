<!-- last_verified: 2026-07-10 -->
# Maintainer Agent

The `genblaze-maintainer` is an autonomous Claude Code agent that operates as the repo's primary guardian. It runs in an isolated git worktree with edit permissions and handles three distinct modes: resolving a specific GitHub issue end-to-end, autonomously discovering and fixing the highest-priority open issue when invoked with no prompt, or running a broad maintenance audit across six quality domains.

- **Agent definition**: `.claude/agents/genblaze-maintainer.md`
- **Checklists**: `.claude/agents/genblaze-maintainer/checklists/`
- **Isolation**: `worktree` — each run gets a clean copy of the repo; no shared state with other agents

---

## Mode Routing

The first thing the agent decides is which mode it's in. These modes are mutually exclusive — audit logic never bleeds into an issue fix.

| Invocation | Mode |
|---|---|
| `#70`, issue URL, "fix issue…" | Issue Resolution |
| No prompt, no issue, no scope | Autonomous Triage |
| "audit", "scan", "check the repo" | Maintenance Audit |

---

## Flow

```mermaid
flowchart TD
    START([Invoked]) --> ORIENT[Orient: read README → ARCHITECTURE → AGENTS\nrun make test / lint / typecheck]
    ORIENT --> ROUTE{Mode?}

    ROUTE -->|Issue given| IR1
    ROUTE -->|No prompt / no scope| AT1
    ROUTE -->|Audit requested| MA[Maintenance Audit]

    %% ── Autonomous Triage ─────────────────────────────────
    AT1[gh issue list --state open\nfetch number, title, labels, age, comments]
    AT1 --> AT2[Score each issue\nsecurity label=4 · bug=3 · feature=1\nage +1pt per 30d · comments +1pt per 5\nskip issues with open PRs or branches]
    AT2 --> AT3[Print ranked top 5 + chosen issue\nhuman can abort before any branching]
    AT3 --> IR1

    %% ── Issue Resolution ──────────────────────────────────
    IR1[Triage\ngh issue view + comments\nclassify: bug / feature / docs / non-code]
    IR1 --> IR1a{Actionable?}
    IR1a -->|No — question / wontfix / duplicate| IR1b[Comment on issue and stop]
    IR1a -->|Yes| IR2[Check for existing PR or branch\ndon't duplicate in-flight work]
    IR2 --> IR3[Branch from origin/main\ngit switch -c type/issue-N-slug origin/main]
    IR3 --> IR4[TDD\nwrite failing test first\nthen smallest fix that passes]
    IR4 --> IR5[Verify green\nmake test · lint · typecheck · coverage ≥70%]
    IR5 --> IR5a{All green?}
    IR5a -->|No| IR5b[Push WIP draft PR\ndescribe blocker and stop]
    IR5a -->|Yes| IR6[Update docs + CHANGELOG\nsame PR as code]
    IR6 --> IR7[Pre-PR review\n3 parallel sub-agents with no knowledge of each other]
    IR7 --> IR7a[Correctness & tests]
    IR7 --> IR7b[Security & invariants]
    IR7 --> IR7c[Architecture & DRY]
    IR7a & IR7b & IR7c --> IR8{Blocking findings?\nP0 or same issue in ≥2 reviews}
    IR8 -->|Yes| IR4
    IR8 -->|No| IR9[git push + gh pr create\nfill PR template with Closes #N\nstop — do not merge]

    %% ── Maintenance Audit ─────────────────────────────────
    MA --> MA1[Phase 1 — Discovery\nread-only: run test + lint + typecheck\nscan for security issues]
    MA1 --> MA2[Phase 2 — Assessment\nfindings per domain · P0 / P1 / P2\ncheck against tech-debt-tracker]
    MA2 --> MA3{Remediation\nauthorized?}
    MA3 -->|No| MA5
    MA3 -->|Yes| MA4[Phase 3 — Remediation\nfix P0 first · one logical commit per fix\nmake test after every change]
    MA4 --> MA5[Phase 4 — Report\nwrite maintenance-report-DATE.md\nupdate tech-debt-tracker\nconfirm make test + lint pass]

    MA2 -.->|Six audit domains| D1[1. Functional Integrity]
    MA2 -.-> D2[2. Security]
    MA2 -.-> D3[3. Code Quality]
    MA2 -.-> D4[4. Documentation]
    MA2 -.-> D5[5. Agent Standards]
    MA2 -.-> D6[6. Dependency Health]
```

---

## Autonomous Triage Protocol

When invoked with no prompt, no issue number, and no audit scope, the agent discovers and prioritizes work itself:

1. **Fetch** all open issues via `gh issue list --state open --json number,title,labels,createdAt,body,comments`.
2. **Score** each issue:
   - Label weight: `security` (4pts) > `bug` (3pts) > `enhancement`/`feature` (1pt) > unlabeled (0pts)
   - Age: +1pt per 30 days open, capped at 4pts
   - Comment volume: +1pt per 5 comments, capped at 3pts
   - Issues with an existing open PR or branch are excluded
3. **Output** the ranked top 5 and the chosen issue before touching any code — the human can abort the agent at this point.
4. **Proceed** with the full Issue Resolution Protocol on the selected issue.

Tie-breaking: equal score → older issue wins; still tied → more comments wins.

---

## Issue Resolution Protocol

Steps map directly to the agent definition's numbered contract:

1. **Triage** — fetch issue + comments; classify and reproduce. Stop without coding if it's a question, wontfix, or duplicate.
2. **Don't duplicate work** — check `gh pr list` and `git branch -a` before touching code.
3. **Branch** — always branch from `origin/main` (worktree HEAD may be stale).
4. **TDD** — failing test first, then smallest idiomatic fix. No opportunistic refactors.
5. **Verify** — `make test`, `make lint`, `make typecheck`, `make coverage` (≥70%). If a Pydantic model changed, regenerate `libs/spec/ts/genblaze.d.ts` via `make ts-types`.
6. **Docs + changelog** — same PR, `[Unreleased]` bullet under the correct package heading.
7. **Commit** — Conventional Commit, imperative subject ≤72 chars, body explains why.
8. **Triangulated review** — three independent sub-agents (correctness, security, architecture). Any P0 or cross-reviewer consensus is blocking; loop back to step 4.
9. **Open PR, stop** — `gh pr create` with `Closes #N`. Never merge, approve, or auto-merge.

---

## Maintenance Audit Domains

| # | Domain | Checklist |
|---|---|---|
| 1 | Functional Integrity | `checklists/functional.md` |
| 2 | Security | `checklists/security.md` |
| 3 | Code Quality | `checklists/code-quality.md` |
| 4 | Documentation | `checklists/documentation.md` |
| 5 | Agent Standards | `checklists/agent-standards.md` |
| 6 | Dependency Health | `checklists/dependencies.md` |

Priority order when remediating: security → functional → code quality → docs → agent standards → deps.

---

## Invariants

The agent must never violate these (inherited from `AGENTS.md`):

- All changes pass `make test`
- Canonical JSON hashing stays deterministic (key sort order, float normalization)
- `canonical_hash` always verifies against re-serialized content
- Provider adapters implement `submit / poll / fetch_output`
- All IDs are UUIDs
- `EmbedPolicy` respected in all embedding paths
- Pydantic v2 models only
- Docs updated in the same PR as code
- Python 3.11+ required
- Providers never store API tokens in manifests
