<!-- last_verified: 2026-06-08 -->
# Maintainer Autonomous Issue→PR Loop

> **Status**: Phase 0 in progress — Dependabot coverage landed (branch
> `ci/dependabot-cover-all-packages`); branch protection + bot identity pending (repo-admin actions).
> **Owner**: jeronimodeleon
> **Extends**: the existing `genblaze-maintainer` subagent. Supersedes nothing.

## Goal

Extend the repo's maintenance automation so it can, unattended:

1. **Triage** open GitHub issues (label, score, dedupe).
2. **Batch** related issues to be worked together.
3. **Branch + draft PR** per batch.
4. **Implement** the fixes (reusing `genblaze-maintainer` as the worker).
5. **Review** the diff (deterministic gate first, then LLM judgment).
6. **Push** the branch and open a **draft** PR.
7. **Loop** to the next batch.
8. **Flag** (not silently apply) dependency/SDK bumps that Dependabot can't handle.

**Hard constraint — human-gated merge**: the loop may push and open PRs, but a human
reviews and merges. The agent must never merge. See [Security](#security--human-gated-merge).

### Explicit override of the global "never push" rule

The user's global working agreement says "never commit/push on my behalf." For **this
automation only**, the user authorized the loop to push branches and open PRs (decision:
2026-06-08). Scoped: it applies to the maintainer-bot identity running this loop, never to
interactive human sessions, and **merge stays 100% human**.

---

## How this plan was revised (red-team pass)

The first draft proposed a full Claude Agent SDK orchestrator (a new `tools/maintainer/`
Python sub-project), a 5-agent reviewer roster, and semantic-similarity batching as Phase 1.
A skeptical-senior-EM review flagged this as **over-engineering Phase 1** — ~17 new files
before a single real issue is processed, breaking the repo's flat `tools/` convention and
its "minimal diffs" ethos. Accepted. The revisions below are folded into the plan:

- **CUT from Phase 1 → moved to Phase 2/3 (earned by evidence):** the SDK orchestrator, the
  separate `pyproject.toml`, the 5-reviewer roster + consensus scoring, semantic batching.
  The SDK remains the documented **target architecture** — adopted when concurrency/state
  genuinely need it, not before.
- **Phase 1 is now a thin proof**: one flat script (`tools/maintainer_loop.py`, matching the
  existing `tools/*.py` convention), label + file-overlap batching only, and **one reused
  `genblaze-maintainer` read-only audit pass** as the LLM review layer.
- **ADDED**: an automated **CI negative test** that the bot identity cannot merge (non-optional);
  explicit "**auto-merge must never be enabled on `main`**"; honesty that **branch protection
  is the only hard technical gate**; a concurrency two-phase-claim prerequisite for Phase 2.
- **Phase 0** (Dependabot extension, branch protection, bot identity) ships as **standalone
  PRs today** — zero dependency on the loop.

---

## Research basis

Synthesized from a verified deep-research pass (21 sources, 25 claims adversarially
verified 3-vote; 22 confirmed, 3 killed). Load-bearing findings:

| Finding | Confidence | Source |
|---|---|---|
| Core agentic loop = gather→act→verify→repeat, with max-iteration stops and human checkpoints before merge. Orchestrator-worker is the recommended pattern for multi-file coding. | high (3-0) | [Anthropic: Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) |
| The **Claude Agent SDK** is recommended over the CLI for CI/CD; first-class **subagents** in isolated context run **concurrently** but **cannot nest**. (This is the Phase 2 target, not Phase 1.) | high (3-0) | [Agent SDK: subagents](https://code.claude.com/docs/en/agent-sdk/subagents), [Building agents with the Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk) |
| Best-practice review = **deterministic checks first**, then **LLM-judge** for fuzzy criteria; a parallel-reviewers-by-dimension roster is a scaling option, NOT a single evaluator-optimizer loop. | high (3-0; evaluator-optimizer-as-the-pattern killed 0-3) | [Anthropic: Building Effective Agents](https://www.anthropic.com/research/building-effective-agents), [calimero ai-code-reviewer](https://github.com/calimero-network/ai-code-reviewer) |
| `claude-code-action` gives event-driven branch/PR mechanics (issue/label/cron/`workflow_dispatch`) but is best for **single-issue** triage/review, not a stateful batched loop. | high (3-0) | [anthropics/claude-code-action](https://github.com/anthropics/claude-code-action) |
| **Killed (0-3)**: that `claude-code-action` *hard-blocks* merge/approve. It does not — you **cannot rely on the tool refusing**. The human gate must be enforced by **withholding capability** (token scope + branch protection). | high | [anthropics/claude-code-action](https://github.com/anthropics/claude-code-action) |
| **Killed (1-2)**: that the action auto-enforces least-privilege. It must be configured explicitly. | — | same |
| Dependency bumps: Dependabot/Renovate own mechanical version pins; agentic detection adds value only for **API migrations / cross-package coordination** Dependabot can't do. | med | [fossa: dependency-upgrade agent](https://fossa.com/blog/fossabot-dependency-upgrade-ai-agent/), [jvt.me: agentic Renovate](https://www.jvt.me/posts/2026/01/23/agentic-renovate/) |

**Caveats (re-check before building):** the CLI `--max-turns` flag has an open bug
([claude-code#1177](https://github.com/anthropics/claude-code/issues/1177), fix PR 1184 open)
— a reason to graduate to the SDK's loop control in Phase 2. The reviewer-roster consensus
evidence leans on a single project, so the roster is a tuning option, not gospel.

---

## Substrate decision

Three candidates were compared. **The Claude Agent SDK orchestrator is the target spine —
but it is earned in Phase 2, not built up front.** Phase 1 proves the loop with the thinnest
viable harness.

| Substrate | Role | Phase |
|---|---|---|
| **CC subagents + skills via `claude -p`** | Reused *inside* the loop as the worker (and, in Phase 1, the reviewer). The existing `genblaze-maintainer.md` + checklists + skills are the implementation units. | 1+ |
| **A thin shell/Python driver** (`tools/maintainer_loop.py`) | **Phase 1 spine** — sequential, human-triggered: list issues → batch → `claude -p` worker → audit pass → `gh pr create --draft` → JSON log. ~150 lines, no new deps. | 1 |
| **Claude Agent SDK (Python orchestrator)** | **Phase 2 spine** — when concurrency, retries, cross-iteration state, and budget control genuinely need it. Idiomatic (genblaze is Python); Anthropic's recommended CI/CD path. | 2 |
| **GitHub Actions / `claude-code-action`** | **Complement** — event-driven *single-issue* front door + the cron/`workflow_dispatch` trigger that launches the loop. Weak for the stateful batched loop. | 2 |

**Rejected:** building the SDK orchestrator before proving the sequential loop (premature
concurrency harness); a pure `claude-code-action` loop (no home for cross-iteration batch
state). The driver/orchestrator is a **dev-only tool** — never a runtime dependency of any
`genblaze-*` wheel, and (matching the flat `tools/` convention) **no separate
`pyproject.toml`**; it uses the root dev environment.

---

## Architecture

Orchestrator-worker + plan-execute-review. **Phase 1 (thin)** and **Phase 2 (SDK)** run the
same logical loop; only the harness and the review fan-out differ.

```
Driver  (Phase 1: tools/maintainer_loop.py  |  Phase 2: SDK orchestrator)
  │
  ├─ 1. TRIAGE     gh issue list --json → score/label
  │                 deterministic pre-filter: drop issues already linked to an open
  │                 maint/* PR (idempotency); honor a `no-bot` opt-out label
  │
  ├─ 2. BATCH      group by LABEL + FILE-OVERLAP only (Phase 1); unbatchable → batch of 1
  │                 caps: max issues/batch (3), max files/batch, max batches/run
  │
  ├─ 3. PER BATCH  (sequential in Phase 1; concurrent worktrees in Phase 2 — see prereq)
  │     ├─ worktree + branch  maint/batch-<slug>
  │     ├─ IMPLEMENT  genblaze-maintainer subagent fixes the batch;
  │     │              runs `make test` + `make lint` after each change (existing invariant)
  │     ├─ REVIEW    deterministic GATE first (make test/lint/typecheck MUST pass),
  │     │            then ONE genblaze-maintainer read-only audit pass over the diff
  │     │            (Phase 2 option: parallel reviewer roster + consensus)
  │     │            ├─ fail → back to IMPLEMENT (bounded: max N=2 iterations)
  │     │            └─ pass → continue
  │     ├─ PUSH      git push + gh pr create --draft   (NO merge capability)
  │     └─ CHECKPOINT  append {batch, issues, branch, pr#, status} to JSON state
  │
  ├─ 4. LOOP       next batch, until max-batches or token budget exhausted
  │
  └─ 5. DEP FLAG   dependency-health check → if Dependabot can't cover it
                   (API migration / umbrella coordination), open a tracking issue.
                   Never silently bump. (Phase 3.)
```

**Subagent roster (markdown agents in `.claude/agents/`, repo-consistent):**

- `genblaze-maintainer` (existing) — the **implement** worker *and*, in Phase 1, the
  **review** pass (read-only, run against the diff). It already covers all six checklist
  domains, so a single audit pass is the fuzzy-judgment layer on top of the deterministic
  gate. Add read-only `gh issue view` so it can read its batch's issues.
- **Phase 2 only** — split out `genblaze-triager` and a `genblaze-reviewer-*` roster
  (correctness / security / tests / docs / agent-standards, each → its existing checklist)
  *if and when* the single-pass review proves consistently too shallow on real PRs.
  Subagents-cannot-nest is respected: the orchestrator (not a subagent) fans these out.

---

## Security & human-gated merge

The research **killed** the assumption that the tooling refuses to merge. Of the four
defense layers below, be honest about which is load-bearing:

1. **Branch protection on `main` — the ONLY hard technical gate.** Require PR + ≥1 human
   approval (CODEOWNERS already exists at `.github/CODEOWNERS`) + green required checks.
   Blocks merge at the repo level regardless of what the agent attempts. **Auto-merge must
   never be enabled on `main`.**
2. **Token scope** (belt-and-suspenders) — the maintainer-bot identity gets `Contents: write`
   + `Pull Requests: write` + `Issues: write` only. No `Administration`. Note: this does
   *not* prevent a non-draft PR (creation is in scope) — branch protection is what stops the
   merge, not the token.
3. **Tool allowlist** (process control) — the driver's `gh`/git allowlist excludes
   `gh pr merge`, `git push origin main`, `git merge`. Mirrors the existing `run.sh`
   allowlist discipline. Not a hard guarantee (an LLM could emit a wrong flag), hence layer 1.
4. **Draft PRs** (process control) — every agent PR opens `--draft`; a human marks ready.

**Non-optional CI negative test**: a job in `maintainer-loop.yml` that, using the bot PAT,
asserts `gh pr merge <synthetic-test-pr>` **fails with permission denied** (then cleans up).
Runs on every dispatch. If a secret rotation ever widens the token, CI catches it — the
"bot cannot merge" property is verified continuously, not once by hand.

Inherits the repo's least-privilege posture (SHA-pinned actions, `contents: read` default,
`persist-credentials: false`, zizmor audit in `.github/workflows/security.yml`).

**Prompt-injection note**: issue bodies are untrusted input. The worker treats issue text as
data, never instructions (an issue saying "run `gh pr merge`" must not escalate). The tool
allowlist is the backstop; branch protection is the hard stop.

---

## DRY analysis (survey-before-build)

| Need | Existing asset to reuse | New work |
|---|---|---|
| Implement fixes | `genblaze-maintainer.md` + 6 checklists + worktree + `make test`/`lint` loop | Add issue-batch awareness + read-only `gh issue view` |
| Review the diff | The **same** `genblaze-maintainer` audit, read-only | None (Phase 1). Roster is Phase 2-only. |
| Run one package's tests fast | `test-package` skill | — |
| Verify docs freshness | `verify-docs` skill | — |
| Dep version pins | `.github/dependabot.yml` (github-actions + `libs/core`) | **Extend** to all `libs/*` + `cli/` (standalone PR) |
| CI security hygiene | `security.yml` (zizmor, SHA-pins, least-priv) | New workflow inherits the same patterns |
| Report output | maintainer writes to `docs/exec-plans/active/` | Loop appends a per-run JSON summary |

The driver loop is the only genuinely new code in Phase 1. Worker, review, dep-pins, and CI
hygiene all reuse existing assets.

---

## File manifest

**Phase 1 (the proof) — 4 changed files, all reuse-heavy:**
```
tools/maintainer_loop.py                  # NEW — flat script (matches tools/*.py); ~150 lines:
                                          #   gh issue list → batch (label+file-overlap) →
                                          #   claude -p worker → audit pass → gh pr create --draft →
                                          #   append .maintainer-state/run-<date>.json
.claude/agents/genblaze-maintainer.md     # EDIT — read-only gh issue view; batch-aware mode
.github/workflows/maintainer-loop.yml     # NEW — workflow_dispatch (+ cron later); SHA-pinned,
                                          #   least-priv, draft PRs, the merge-negative-test job
docs/features/maintainer-loop.md          # NEW — how to run it + the human-gate guarantees
.gitignore                                # EDIT — ignore .maintainer-state/
```

**Phase 0 — standalone, shippable today, no loop dependency:**
```
.github/dependabot.yml                    # EDIT — extend to all libs/* + cli/  (~12 lines)
(repo settings)                           # branch protection on main; provision bot identity
```

**Phase 2/3 (earned later):**
```
tools/maintainer/  (SDK orchestrator)     # IF concurrency/state require it — no pyproject; root dev env
.claude/agents/genblaze-triager.md        # IF triage warrants a dedicated agent
.claude/agents/genblaze-reviewer-*.md     # IF single-pass review proves too shallow (5 dims)
.github/workflows/claude-issue-triage.yml # OPTIONAL — claude-code-action single-issue front door
```

---

## Issue batching signals

**Phase 1 — deterministic, zero-cost signals only:**

1. **Label** — same area label (`area/connectors`, `area/core`, `docs`, `security`).
2. **File overlap** — issues whose referenced/likely-touched files overlap → batch together
   (lower merge-conflict risk than splitting across PRs).

Issues that share neither → **batch of 1**. Never mix `security` with unrelated `docs` —
batches stay single-purpose.

**Caps** (in the driver): max issues/batch = 3 to start, max files/batch, max batches/run.

**Deferred to Phase 3** — semantic-similarity clustering (embeddings). It adds an API/dep
surface and only matters if the batch-of-1 rate on unlabeled issues proves unacceptably high.
Don't pay for it until the data says so.

---

## State, checkpointing, idempotency

- **State store**: JSON under `.maintainer-state/` (gitignored), append-per-run, recording
  each batch's issues, branch, PR number, review verdict, status. Enables resume + audit.
- **Idempotency** — before forming a batch, query open `maint/*` PRs and their linked issues;
  **skip any issue already in flight**. Deterministic branch names (`maint/batch-<slug>`) mean
  a re-run targets the same branch rather than spawning a parallel one.
- **Loop termination** — stop at `max_batches`, on budget exhaustion, or when triage returns
  zero eligible issues. Never unbounded.

**Phase 2 concurrency prerequisite (hard):** the live-`gh`-query idempotency check has a
race — two concurrent batches can both query, both see no overlap (neither has pushed yet),
and both claim the same issue. A serialized push does **not** fix the double-claim at triage
time. **Before** Phase 2 adds concurrent worktrees, implement a two-phase claim (a lock file
or a claim marker — e.g. a placeholder draft PR / a `bot-claimed` label written atomically at
batch-formation time). This is a design gate for Phase 2, not a Phase 2 nice-to-have.

---

## Cost & token controls

- **Model tiering** — triage/batching on a cheaper model; implementation + the audit pass on
  the worker's configured model. Reserve opus for escalation.
- **Hard budget** — a per-run token ceiling; the driver stops cleanly when hit (in-progress
  batch is checkpointed, not lost).
- **Bounded review iterations** — max N=2 implement↔review cycles per batch, then push as
  draft with the open review notes attached for the human.
- **Concurrency cap** (Phase 2) — N parallel worktrees, bounded by CI runner cores.

---

## Dependency-bump detection (complement, don't duplicate Dependabot)

- **Phase 0 (standalone PR)**: extend `.github/dependabot.yml` from `libs/core` to all
  `libs/*` + `cli/`. Closes most of the mechanical gap with zero agent involvement.
- **Phase 3 agentic value-add**: a dependency-health check (reusing
  `checklists/dependencies.md`) that flags what Dependabot can't — SDK **API migrations**
  needing code changes, cross-package umbrella-version coordination, deprecations. It opens a
  **tracking issue**, never a silent bump. Dependabot pins versions; the agent handles
  migrations.

---

## Phased build sequence

**Phase 0 — Foundations (ship as standalone PRs today; no loop dependency).**
1. ✅ **Done** — `dependabot.yml` extended from `libs/core` to all 16 Python packages via a
   `directories` glob (auto-covers future scaffolded connectors); `genblaze-*` internal deps
   ignored so Dependabot doesn't fight the release wave. Branch `ci/dependabot-cover-all-packages`.
2. ⏳ Enable branch protection on `main` (PR + 1 approval + green checks, no auto-merge) —
   **the merge gate.** Repo-admin action; see commands below.
3. ⏳ Provision the maintainer-bot identity, least-privilege scope (no merge). Repo-admin action.

**Phase 1 — The thin loop, human-triggered (prove it produces useful PRs).**
4. Add batch-aware + read-only `gh issue view` mode to `genblaze-maintainer.md`.
5. Build `tools/maintainer_loop.py`: triage→batch(label+file-overlap)→implement(reuse
   maintainer)→deterministic gate→one maintainer audit pass→`gh pr create --draft`→JSON log.
   **Sequential. No SDK. No reviewer roster. No semantic batching.**
6. `maintainer-loop.yml` with `workflow_dispatch` + the **CI merge-negative-test** job.
7. Run locally against a handful of real issues; tune caps; confirm draft PRs + the gate.

**Phase 2 — Graduate to the SDK + automate + parallelize (only once Phase 1 proves value).**
8. Port the loop into a Claude Agent SDK orchestrator for real state/retry/budget control.
9. Add cron trigger; concurrent batches via worktrees — **gated on the two-phase-claim prereq.**
10. (If single-pass review is too shallow) add the reviewer roster + consensus.
11. (Optional) `claude-code-action` for event-driven single human-filed issues.

**Phase 3 — Dependency intelligence & batching polish.**
12. Agentic dependency-health check complementing Dependabot.
13. Semantic-similarity batching if the batch-of-1 rate demands it.

Each phase ships value alone; later phases are graduations, not prerequisites.

---

## Test strategy

- **Unit** (`tools/` test file, matching repo convention): batching (label + file-overlap +
  caps), state round-trip + resume, idempotency filter (skip in-flight issues). Deterministic
  — mock the `gh`/`claude -p` boundary, no live LLM calls.
- **Dry-run integration**: driver against a fixture issue set with the LLM/`gh` boundary
  mocked — asserts the expected batches, branch names, and that it *would* open the right
  draft PRs without touching GitHub.
- **CI merge-negative-test (non-optional)**: on every `maintainer-loop.yml` dispatch, assert
  the bot PAT **cannot** merge a synthetic test PR (non-zero exit), then clean up.
- **Live smoke (manual, Phase 1)**: one real batch end-to-end on a throwaway branch.
- All gated by `make test`/`make lint`.

---

## Risks & open questions

| Risk / question | Mitigation / current lean |
|---|---|
| **Over-engineering** (the original Phase-1 sin) | Phase 1 is now one script + reuse; SDK/roster/semantic batching are earned later. |
| **Bad batching** → noisy, conflict-prone PRs | Tight caps (3 issues, single-purpose); file-overlap as the primary signal; batch-of-1 default. |
| **Reviewer rubber-stamping** | Deterministic gate (`make test`/`lint`) runs *first* and is non-negotiable; the LLM pass only adds fuzzy judgment. |
| **Human gate bypass** (non-draft PR, widened token) | Branch protection is the only hard gate + no auto-merge + the CI merge-negative-test catches token widening. |
| **Duplicate/conflicting PRs** | Live in-flight check + deterministic branch names (Phase 1). **Phase 2 needs the two-phase-claim prereq** before concurrency. |
| **Prompt injection via issue text** | Treat issue bodies as data; tool allowlist backstop; no merge capability. |
| **Runaway token spend** | Per-run budget ceiling + bounded review iterations + model tiering. |
| **`--max-turns` CLI bug** | Phase 1 sequential mode is tolerant; graduate to SDK loop control in Phase 2; re-check [#1177](https://github.com/anthropics/claude-code/issues/1177). |
| Where does the SDK load markdown subagents from? | Phase 2 question; verify `.claude/agents/*.md` loading then; fall back to SDK-defined subagents. |

---

## Definition of done (Phase 1)

- `tools/maintainer_loop.py` runs locally, takes N open issues, produces coherent
  label/file-overlap batches, opens one **draft** PR per batch on a `maint/*` branch, each
  with `make test`/`lint` green.
- The bot identity provably **cannot** merge — the **CI merge-negative-test passes**.
- Unit + dry-run integration tests green under `make test`.
- `docs/features/maintainer-loop.md` documents how to run it and the human-gate guarantees.
