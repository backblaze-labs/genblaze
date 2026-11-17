---
name: verify
description: Prove a genblaze change is done before opening a PR. Runs `make verify` (lint, typecheck, full tests, docs lint, end-to-end CLI smoke) and formats the evidence for the PR's Testing section. Use before every PR and after any fix that touches code, docs, or CI.
allowed-tools: Bash(make verify:*) Bash(make docs-lint:*) Bash(make e2e-smoke:*) Bash(git status:*) Bash(git diff:*) Read
---

# Verify the change

`make verify` is the single source of truth. CI runs the same targets; this
skill only runs it and reports. Never substitute a narrower command and call
it verified.

1. Run `make verify` from the repo root.
   - `docs-lint` diffs against `origin/main`. On a stale clone, pass
     `DOCS_BASE=<ref>` rather than skipping it.
2. If it fails, fix the root cause and re-run.
   - After **three** failed fix attempts on the same check, stop and ask the
     human. Don't keep iterating.
   - Never skip, `xfail`, add sleeps to, or retry a flaky test until it passes.
     Find the cause, or delete the test and say why in the PR.
   - Never edit `tools/check_docs.py`, `tools/e2e_smoke.py`, or REVIEW.md to
     make your own change pass.
3. On success, give the user a block for the PR's **Testing** section:

   ````
   ### Testing
   - `make verify`: passed
   - e2e: <the "OK: e2e smoke passed ..." line>
   - docs lint: <the "docs lint: N error(s), M warning(s)" line>
   - Not covered: <live provider calls, ts-types-check (CI-only), anything else>
   ````

   Report the real output only. If something was not run, say so.
