<!-- last_verified: 2026-07-21 -->
# Maintainer Batch Issue Orchestration

> **Status**: Implemented — deterministic core + workflows landed; runtime
> (multi-agent) execution validated only by design + the tested core, not yet by
> a live batch run.
> **Owner**: jeronimodeleon
> **Extends**: [`maintainer-autonomous-issue-to-pr-loop.md`](maintainer-autonomous-issue-to-pr-loop.md)
> — this realizes the "triage → batch → branch → implement → review → push → loop"
> vision with a concrete, human-gated, two-workflow architecture. Reuses the
> existing `genblaze-maintainer` subagent unchanged as the per-cluster executor.

## Goal

Let a maintainer own the **entire open-issue list**: review all open issues,
group the ones that share code, order them by dependency, and — after explicit
human approval — resolve each group into a merge-ready PR. It must be safe
(no data loss, human-gated merge), production-grade (not patchy), and scalable.

## Architecture: two layers, gate between them

The maintainer's single-issue contract is excellent and unchanged; the new work
is a **portfolio layer above it**, deliberately not crammed into agent prose.

- **Deterministic core (tested Python)** — the logic that must be correct lives
  in `tools/batch_*.py`, under `make test`, not in an LLM prompt:
  - `batch_cluster.py` — hot-file exclusion, file-overlap connected components,
    explicit-dependency edges, DAG layering, cluster-size cap. Pure function
    (`build_plan`) + CLI. Oversized/overlapping components serialize; dependent
    layers defer.
  - `batch_plandoc.py` — writes the plan doc with an embedded `origin/main` SHA +
    a content **approval token** (`sha256(base_sha + canonical(clusters))`), and
    a `validate` command that refuses execution on token mismatch (hand-edit),
    SHA drift (main advanced), or a closed planned issue.
  - `batch_gitsteward.py` — `preflight` (never switches/stashes/commits; FF `main`
    only when not checked out and strictly behind) and `gc` (prunes only merged,
    pushed leftover branches with `git branch -d`, never `-D`; skips dirty
    worktrees; reports retained).
- **Orchestration (two workflows)** — thin scripts that call the tested CLIs and
  fan out agents:
  - `.claude/workflows/batch-plan.js` — preflight → scout each issue (the one
    fuzzy step: estimate touched files) → cluster → write plan doc → advisory
    red-team → **stop**.
  - `.claude/workflows/batch-execute.js` — validate against live state → optional
    `dryRun` dispatch preview → one `genblaze-maintainer` per executable cluster
    (isolated worktree, base `origin/main`) → verify each PR closes its members →
    `gc`.

## Key decisions (and why)

- **Two workflows, not one** — a workflow cannot pause for human input, so the
  approval gate is the boundary between `batch-plan` and `batch-execute`. The
  gate is *enforced* by the SHA+token+drift validator, not by convention.
- **One PR per cluster, capped** — related issues combine into one reviewable PR
  (`Closes #a`, `Closes #b`) only when genuinely inseparable and under a size
  cap; above the cap they split into sequential PRs. Keeps diffs reviewable.
- **No stacked branches in v1** — dependent clusters are deferred to a re-plan
  after prerequisites merge, rather than branched off each other. Stacking breaks
  when a human squash-merges the prerequisite (stale base, phantom diffs).
- **Clustering is advisory** — its input is an LLM file estimate, so it can miss
  a conflict. Backstops: hot-file exclusion, the human gate, and the executor's
  real-conflict → draft-PR fallback.
- **Never destroys unshipped work** — preflight and `gc` only ever do safe,
  reversible git operations; anything risky is reported, not performed.

## Tests

- `tools/tests/test_batch_cluster.py` — disjoint / overlapping / hot-file-only /
  dependency-chain / cycle / oversized / skipped / normalization (13).
- `tools/tests/test_batch_plandoc.py` — round-trip + every rejection path
  (tamper, advanced main, closed issue) + token stability (9).
- `tools/tests/test_batch_gitsteward.py` — real throwaway repos: clean/dirty/
  behind/checked-out/diverged/no-local-main preflight; gc merged/squash/in-flight/
  non-matching/worktree-clean/worktree-dirty (12).

## Follow-ups

- Runtime shakeout: run `batch-plan` then `batch-execute --dryRun` against the
  live issue list and confirm the dispatch plan reads correctly before a first
  real execution.
- Consider symbol/import-graph edges in clustering if file-overlap false
  negatives bite in practice (intentionally deferred as over-engineering for v1).
- Permissions: `batch-execute` needs the same git/gh permissions as single-issue
  Issue Resolution mode (see the maintainer README).
