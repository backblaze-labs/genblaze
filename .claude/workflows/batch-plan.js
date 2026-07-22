// Batch Maintainer — PLAN phase (read-only, stops for human approval).
//
// This is the first half of the two-workflow batch maintainer. It reviews every
// open GitHub issue, has each scouted for the files it would touch, then feeds
// those estimates through the DETERMINISTIC, unit-tested clustering + plan-doc
// tools in `tools/batch_*.py`. It writes an approval-gated plan document and
// STOPS. No branches, no code, no PRs — the human reads the plan and, if happy,
// runs `batch-execute` against it.
//
// Why the heavy lifting lives in Python, not here: clustering and the approval
// token must be deterministic and testable (`pytest tools/tests/`). The agents
// below are thin — they scout (the one irreducibly fuzzy step) and act as pipes
// to the tested CLIs. See `.claude/agents/genblaze-maintainer.md`.

export const meta = {
  name: 'batch-plan',
  description:
    'Review all open issues, cluster & dependency-order them via the tested Python core, write an approval-gated plan doc, then stop for human review',
  phases: [
    { title: 'Preflight', detail: 'clean tree + sync main safely' },
    { title: 'Scout', detail: 'estimate touched files per open issue' },
    { title: 'Plan', detail: 'deterministic clustering + plan doc' },
    { title: 'Red-team', detail: 'adversarial review of the plan' },
  ],
}

const PREFLIGHT_SCHEMA = {
  type: 'object',
  additionalProperties: true,
  required: ['ok', 'main_action', 'messages'],
  properties: {
    ok: { type: 'boolean' },
    main_action: { type: 'string' },
    dirty: { type: 'boolean' },
    messages: { type: 'array', items: { type: 'string' } },
  },
}

const ISSUES_SCHEMA = {
  type: 'object',
  required: ['issues'],
  properties: {
    issues: {
      type: 'array',
      items: {
        type: 'object',
        required: ['number', 'title'],
        properties: {
          number: { type: 'integer' },
          title: { type: 'string' },
          labels: { type: 'array', items: { type: 'string' } },
        },
      },
    },
  },
}

// Matches the scout row shape consumed by tools/batch_cluster.py.
const SCOUT_SCHEMA = {
  type: 'object',
  required: ['number', 'touched_files', 'deps'],
  properties: {
    number: { type: 'integer' },
    type: { type: 'string' },
    touched_files: { type: 'array', items: { type: 'string' } },
    deps: { type: 'array', items: { type: 'integer' } },
    skip_reason: { type: ['string', 'null'] },
  },
}

const PLAN_SCHEMA = {
  type: 'object',
  required: ['doc_path', 'executable_clusters', 'deferred_clusters'],
  properties: {
    doc_path: { type: 'string' },
    base_sha: { type: 'string' },
    executable_clusters: { type: 'integer' },
    deferred_clusters: { type: 'integer' },
    summary: { type: 'string' },
  },
}

// How many issues to scout at most (blast-radius cap). Override via args.limit.
const LIMIT = (args && args.limit) || 40

phase('Preflight')
const pre = await agent(
  `Run EXACTLY this and nothing else, then return its parsed JSON:\n` +
    `  python tools/batch_gitsteward.py preflight\n` +
    `Do not modify any files. Do not switch branches. Just report what the ` +
    `command output. The command exits non-zero when the repo is not safe to ` +
    `start a batch (dirty tree, main behind while checked out, diverged).`,
  { label: 'preflight', phase: 'Preflight', schema: PREFLIGHT_SCHEMA, effort: 'low' },
)

if (!pre || !pre.ok) {
  const msgs = (pre && pre.messages) || ['preflight failed']
  log(`Preflight blocked the batch: ${msgs.join(' | ')}`)
  return {
    stopped: 'preflight',
    reason: msgs,
    note: 'Fix the working tree / main branch state, then re-run batch-plan.',
  }
}
log(`Preflight OK (main: ${pre.main_action}).`)

phase('Scout')
const listed = await agent(
  `List open issues as JSON. Run:\n` +
    `  gh issue list --state open --json number,title,labels --limit ${LIMIT}\n` +
    `Return {"issues": [...]} with labels flattened to an array of label names.`,
  { label: 'list-issues', phase: 'Scout', schema: ISSUES_SCHEMA, effort: 'low' },
)
const issues = (listed && listed.issues) || []
if (issues.length === 0) {
  log('No open issues to plan.')
  return { stopped: 'empty', reason: ['no open issues'] }
}
log(`Scouting ${issues.length} open issue(s)…`)

// One cheap scout per issue, in parallel. Read-only: read the issue, look for an
// existing PR/branch (=> skip_reason), estimate the files a fix would touch, and
// note any explicit "depends on #N" links. This is the only non-deterministic
// step; everything downstream is deterministic Python.
const scout = (
  await parallel(
    issues.map((iss) => () =>
      agent(
        `You are scouting GitHub issue #${iss.number} ("${iss.title}") in the ` +
          `genblaze repo to feed the deterministic clustering tool. READ-ONLY.\n` +
          `1. \`gh issue view ${iss.number} --comments\` to understand it.\n` +
          `2. Check for existing work: \`gh pr list --search "closes #${iss.number}"\` ` +
          `and \`git branch -a --list "*issue-${iss.number}*"\`. If found, set ` +
          `skip_reason and empty touched_files.\n` +
          `3. Otherwise estimate the repo-relative files a fix would touch (grep/` +
          `glob only — do NOT edit). Be conservative and specific.\n` +
          `4. Record explicit dependencies (issue numbers this one says it depends ` +
          `on / is blocked by) in deps.\n` +
          `Return the scout row for issue ${iss.number}.`,
        {
          label: `scout:#${iss.number}`,
          phase: 'Scout',
          schema: SCOUT_SCHEMA,
          effort: 'low',
        },
      ),
    ),
  )
).filter(Boolean)

phase('Plan')
// Pipe the scout rows through the tested CLIs. This agent is a dumb pipe: it
// writes the scout JSON, runs batch_cluster.py, captures origin/main's SHA, and
// runs batch_plandoc.py write. It must not hand-edit any of the JSON.
const plan = await agent(
  `Produce the batch plan document using ONLY the provided tools. Steps:\n` +
    `1. Write this scout JSON to a temp file (e.g. /tmp/scout.json):\n` +
    '```json\n' +
    JSON.stringify(scout, null, 2) +
    '\n```\n' +
    `2. Cluster: \`python tools/batch_cluster.py --scout /tmp/scout.json > /tmp/plan.json\`\n` +
    `3. Capture base SHA: \`git rev-parse origin/main\` and today's date: \`date +%F\`.\n` +
    `4. Write the doc:\n` +
    `   \`python tools/batch_plandoc.py write --plan /tmp/plan.json --sha <SHA> ` +
    `--date <DATE> --out docs/exec-plans/active/batch-plan-<DATE>.md\`\n` +
    `Do not alter the JSON between steps. Return doc_path, base_sha, and counts ` +
    `of executable vs deferred clusters from /tmp/plan.json, plus a one-paragraph ` +
    `summary of what would run now.`,
  { label: 'write-plan', phase: 'Plan', schema: PLAN_SCHEMA },
)

if (!plan || !plan.doc_path) {
  log('Planner did not produce a plan doc.')
  return { stopped: 'plan', reason: ['clustering/plan-doc step failed'] }
}

phase('Red-team')
// Advisory only — findings are appended to the doc for the human, not blocking.
const redteam = await agent(
  `Adversarially review the batch plan at ${plan.doc_path}. You are a skeptical ` +
    `senior maintainer. Focus on: clusters that will conflict despite touching ` +
    `disjoint files (shared imports, new sibling dirs, a core signature change), ` +
    `oversized combined PRs, and wrong dependency ordering. For each concern give ` +
    `the specific issues and a one-line fix. Append a "## Red-team review" section ` +
    `to the doc with your findings (or "No blocking concerns."). Return a short ` +
    `summary string.`,
  { label: 'red-team', phase: 'Red-team' },
)

return {
  doc_path: plan.doc_path,
  base_sha: plan.base_sha,
  executable_clusters: plan.executable_clusters,
  deferred_clusters: plan.deferred_clusters,
  summary: plan.summary,
  red_team: redteam,
  next: `Review ${plan.doc_path}. To execute the approved clusters, run the ` +
    `batch-execute workflow with args {"docPath": "${plan.doc_path}"}. ` +
    `Add "dryRun": true first to preview the dispatch without opening PRs.`,
}
