---
type: Plan
title: Release standards with Changie and signed tags
description: Preserve independent package releases while adding generated notes, trusted tags, and milestone readiness.
status: draft
stale_after: '2027-02-17T00:00:00Z'
owner: '@jeronimodeleon'
---

# Release standards

Tracking issue: [#354](https://github.com/backblaze-labs/genblaze/issues/354).

## Findings and approach

Changie components can group fragments by package without rewriting package
versions. Migrate existing version sections without changing their text; convert
pending notes into fragments. Keep prepare_release.py as the version/floor engine.

Use Git SSH tag signatures with a reviewed, repository-local allowed-signers
file. Read the production trust list from origin/main, require the tag's commit
on that branch, and bind every downstream checkout to the validated commit.
Reject missing, unsigned, untrusted, and off-main production tags. Legacy tags
remain historical; dry-run dispatches do not claim to verify them.

Require the wave's vX.Y.Z milestone to be closed with no open issues or PRs before
production. Assign release-bound issues and PRs during planning; defer unrelated
open work to another milestone. The next wave name remains a human decision.

Retain release: published. Compare the present Release → publish ordering with
tag → publish → Release in the PR, recommend the latter, and request a human
choice before a separate trigger migration.

## Self-review

A major risk is weakening dry-run gates to tolerate pending notes or broken
published pins. Keep all four existing gates, and exercise a separately recorded
release-candidate snapshot with batched notes for the dispatch. Keep the
build-only dry run that skips all registry uploads, preserving merged PR #352.
Do not change production OIDC, attestations, or package versions.

Trust must not come from the unverified tag itself. Tests use real SSH-signed
tags, including a forged allowlist in an off-main commit, and exercise the actual
Git verification command. Milestone tests cover missing, open and closed states.
Changie tests check generated output and preservation of package versions.

## Work and verification

1. Add regression tests, observe the intended failures, then implement controls.
2. Add Changie config, migrate history, and document fragment/batch commands.
3. Integrate tag, milestone and generated-note checks into existing gates;
   preserve pinned actions and credential boundaries.
4. Add RELEASE.md and update contributor/release guidance and CODEOWNERS.
5. Run focused checks, make pre-release (includes make verify's checks), and
   SDK panel review. Push and run the non-publishing workflow_dispatch against
   a recorded candidate. Open a PR with the trigger decision and real evidence.

## Baseline

The source checkout is 09836ec. GitHub CI passes there. Initial local setup with
uv could not resolve hume>=0.13.13,<1 because the available satisfying release
was yanked. An explicit 0.13.13 test-environment install is diagnostic only;
release-smoke retains its normal resolver and may expose the same baseline gap.

## Upstream reconciliation

Rebased onto 7672b92, including #352 (build-only dry runs, no TestPyPI) and
#355 (warning-only first-publish readiness). Preserve both, including the
pypi-new-projects target and package-list parity tests. The requested validation
runs with dry_run=true and uploads nothing; it does not use TestPyPI after #352.

Preserve #356 core fixes and migrate its two pending notes into fragments.

## Workflow commit binding

Candidate and downstream checkouts use the workflow event's immutable SHA.
The optional dispatch ref asserts the selected workflow branch/tag or commit;
it cannot select a second code revision. Run previews from the prepared
candidate branch and production dispatches from the signed release tag.
The production signature, milestone checks and release trigger are retained.
