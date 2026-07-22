// Batch Maintainer — EXECUTE phase (runs only against an approved plan).
//
// The second half of the two-workflow batch maintainer. Given the plan doc that
// `batch-plan` produced and the human approved, it: re-validates the plan against
// LIVE repo state (aborting if main advanced or the issue set drifted — the
// approval gate is enforced, not assumed), optionally prints a dry-run dispatch,
// then dispatches ONE genblaze-maintainer executor per executable cluster in
// isolated worktrees. Each executor runs the unchanged Issue Resolution Protocol
// (TDD → verify → triangulated review → one merge-ready PR → STOP). Finally it
// tidies leftover worktrees/branches via the tested git steward.
//
// v1 scope: only clusters marked `executable` in the plan run (dependency layer 0
// + within the size cap). Deferred clusters are NOT stacked — they wait for a
// re-plan after their prerequisites merge. Nothing is ever auto-merged.
//
// Invoke with args: { docPath: "docs/exec-plans/active/batch-plan-YYYY-MM-DD.md",
//                      dryRun?: true }

export const meta = {
  name: 'batch-execute',
  description:
    'Validate an approved batch plan against live state, then dispatch one genblaze-maintainer executor per executable cluster into merge-ready PRs (never merges)',
  phases: [
    { title: 'Validate', detail: 'drift-check the approved plan' },
    { title: 'Execute', detail: 'one maintainer per cluster → PR' },
    { title: 'Verify', detail: 'each PR closes its member issues' },
    { title: 'Cleanup', detail: 'tidy worktrees + merged branches' },
  ],
}

const VALIDATE_SCHEMA = {
  type: 'object',
  required: ['valid', 'clusters'],
  properties: {
    valid: { type: 'boolean' },
    reason: { type: 'string' },
    base_sha: { type: 'string' },
    clusters: {
      type: 'array',
      items: {
        type: 'object',
        required: ['slug', 'issues', 'pr_mode'],
        properties: {
          slug: { type: 'string' },
          issues: { type: 'array', items: { type: 'integer' } },
          pr_mode: { type: 'string' },
        },
      },
    },
  },
}

const EXEC_SCHEMA = {
  type: 'object',
  required: ['slug', 'status'],
  properties: {
    slug: { type: 'string' },
    status: { type: 'string' }, // pr | draft | blocked
    pr_url: { type: ['string', 'null'] },
    issues: { type: 'array', items: { type: 'integer' } },
    note: { type: 'string' },
  },
}

const docPath = args && args.docPath
if (!docPath) {
  throw new Error('batch-execute requires args.docPath (the approved plan doc).')
}
const dryRun = !!(args && args.dryRun)

phase('Validate')
// Enforce the approval gate: re-derive live state and run the tested validator.
// It aborts on a hand-edited doc (token mismatch), an advanced main (SHA drift),
// or a planned issue that has since closed.
const check = await agent(
  `Validate the approved batch plan before any execution. Steps:\n` +
    `1. \`git fetch origin --quiet\` then capture live SHA: \`git rev-parse origin/main\`.\n` +
    `2. Live open issues: \`gh issue list --state open --json number --limit 200\` ` +
    `-> a JSON array of numbers, e.g. [70,71].\n` +
    `3. Run:\n` +
    `   \`python tools/batch_plandoc.py validate --doc ${docPath} --sha <LIVE_SHA> ` +
    `--open-issues '<JSON_ARRAY>'\`\n` +
    `   If it exits non-zero, set valid=false and put its stderr in reason.\n` +
    `4. If valid, parse the BATCH-PLAN-META block in ${docPath} and return the ` +
    `clusters whose "executable" is true (slug, issues, pr_mode) in \`clusters\`.\n` +
    `Do not modify the doc.`,
  { label: 'validate-plan', phase: 'Validate', schema: VALIDATE_SCHEMA, effort: 'low' },
)

if (!check || !check.valid) {
  const reason = (check && check.reason) || 'validation failed'
  log(`Plan rejected — not executing: ${reason}`)
  return { stopped: 'validation', reason }
}

const clusters = check.clusters || []
if (clusters.length === 0) {
  log('No executable clusters in the approved plan.')
  return { stopped: 'empty', reason: 'nothing executable' }
}

// Dry-run: show exactly what would be dispatched, spawn nothing.
const dispatch = clusters.map((c) => ({
  slug: c.slug,
  branch: c.issues.length > 1 ? `cluster/${c.slug}` : `fix/issue-${c.issues[0]}`,
  base: 'origin/main',
  closes: c.issues.map((n) => `Closes #${n}`).join(', '),
  pr_mode: c.pr_mode,
}))
log(
  `Dispatch plan (${dispatch.length} cluster(s)):\n` +
    dispatch.map((d) => `  • ${d.branch} (base ${d.base}) → ${d.closes}`).join('\n'),
)
if (dryRun) {
  return { dry_run: true, dispatch }
}

phase('Execute')
// One maintainer per cluster, in isolated worktrees so parallel clusters never
// collide. Each runs the full unchanged protocol and stops at a merge-ready PR.
const results = (
  await parallel(
    clusters.map((c) => () =>
      agent(
        `You are the Genblaze Maintainer in ISSUE RESOLUTION mode for a CLUSTER of ` +
          `issues: ${c.issues.map((n) => `#${n}`).join(', ')}.\n\n` +
          `Follow your Issue Resolution Protocol exactly, with these cluster rules:\n` +
          `- Branch off origin/main as \`${c.issues.length > 1 ? `cluster/${c.slug}` : `fix/issue-${c.issues[0]}`}\`. ` +
          `Do NOT branch off any other cluster's branch (no stacking).\n` +
          `- Resolve ALL listed issues on this one branch with TDD, smallest ` +
          `idiomatic fixes. If they turn out NOT to be safely separable within a ` +
          `reviewable diff, or you hit a real conflict, open a DRAFT PR explaining ` +
          `why instead of a broken combined PR.\n` +
          `- Verify (make test/lint/typecheck/coverage), update docs + CHANGELOG, ` +
          `run the triangulated 3-reviewer check, resolve blocking findings.\n` +
          `- Open ONE PR whose Related section lists ${c.issues.map((n) => `Closes #${n}`).join(', ')}. ` +
          `STOP for human review — never merge, auto-merge, or approve.\n` +
          `Return slug="${c.slug}", the PR url, status (pr|draft|blocked), and a note.`,
        {
          label: `exec:${c.slug}`,
          phase: 'Execute',
          agentType: 'genblaze-maintainer',
          isolation: 'worktree',
          schema: EXEC_SCHEMA,
        },
      ),
    ),
  )
).filter(Boolean)

phase('Verify')
// Post-condition: every opened PR must actually reference each member issue —
// otherwise an issue silently stays open after a "successful" batch.
const verified = await parallel(
  results
    .filter((r) => r.pr_url)
    .map((r) => () =>
      agent(
        `Check that PR ${r.pr_url} closes every one of these issues in its body: ` +
          `${r.issues.map((n) => `#${n}`).join(', ')}. Run \`gh pr view ${r.pr_url} ` +
          `--json body\` and confirm a "Closes #N" (or "Fixes/Resolves #N") exists ` +
          `for each. Return {slug:"${r.slug}", ok:true|false, missing:[...]}.`,
        {
          label: `verify:${r.slug}`,
          phase: 'Verify',
          effort: 'low',
          schema: {
            type: 'object',
            required: ['slug', 'ok'],
            properties: {
              slug: { type: 'string' },
              ok: { type: 'boolean' },
              missing: { type: 'array', items: { type: 'integer' } },
            },
          },
        },
      ),
    ),
).then((v) => v.filter(Boolean))

phase('Cleanup')
// Deterministic, conservative tidy: prune merged leftover branches/worktrees.
// Never force-deletes; skips dirty worktrees; reports anything it retains.
const cleanup = await agent(
  `Run EXACTLY this and return its parsed JSON, changing nothing else:\n` +
    `  python tools/batch_gitsteward.py gc\n` +
    `This prunes only worktrees/branches whose PRs are already merged.`,
  { label: 'gc', phase: 'Cleanup', effort: 'low' },
)

const missingCloses = verified.filter((v) => !v.ok)
return {
  opened: results.map((r) => ({ slug: r.slug, status: r.status, pr_url: r.pr_url })),
  pr_body_check_failures: missingCloses,
  cleanup,
  note:
    'All PRs are merge-ready and awaiting human review — nothing was merged. ' +
    (missingCloses.length
      ? 'WARNING: some PRs are missing Closes links — fix before merging.'
      : ''),
}
