<!-- last_verified: 2026-05-11 -->
# Release 0.3.0 — readiness sweep

> Final coordination of the model-registry-decoupling rollout. The
> code is shipped (PR #1-#14), production-hardened
> ([`model-registry-decoupling-hardening.md`](model-registry-decoupling-hardening.md)),
> and deeply reviewed. This plan closes the remaining
> release-mechanics gaps so the `0.3.0` tag actually installs against
> PyPI.

## Why this plan exists

Post-hardening review surfaced 5 release-blocking gaps the implementation
work didn't touch. The earlier hardening review verified that the
code is production-grade; this plan verifies that the **release
pipeline** is production-grade.

Three industry-standard practices were also missing and are added here:
release candidate before final, post-build wheel install smoke test,
and TS-types drift verification.

## Scope

| Tier | Items | Why |
|---|---|---|
| 0 (pre-release smoke) | wheel install test, TS-types verify | Catches release breakage before any tag |
| 1 (must-fix) | core version, pyproject pin sweep, connector versions, umbrella job, CHANGELOG cut, example redesign, RC strategy | Without these, `pip install` fails or examples crash |
| 2 (release hygiene) | NVIDIA in CI/release, PyPI metadata, soft-drift example, stamp bump, CHANGELOG gate | Won't block install but should ship with the release |
| 3 (deferred) | Auto-discovered release matrix, post-publish smoke test, version-consistency CI check | Process improvements; open as follow-up issues |

Explicitly out of scope: sample-app coordination in separate repos
(`genblaze-gmicloud-pipeline`, `nvidia-nemotron-genblaze-b2`).

---

## Tier 0 — pre-release smoke

### M3 — Verify TS types didn't drift

The hardening pass didn't change manifest schema, but the SDK ships
generated TS types (`libs/spec/ts/genblaze.d.ts`). Run `make ts-types`
before tagging; the resulting diff must be empty. If it isn't, the
`@genblaze/spec` npm package needs a coordinated bump.

### M2 — Wheel install smoke test

CI currently uses `pip install -e` which **bypasses version constraint
checking entirely**. There is no existing test that proves a freshly
built `genblaze-core==0.3.0` wheel installs against `genblaze-openai==0.3.0`
from PyPI metadata constraints.

Add `make release-smoke` (and a CI job) that:
1. `python -m build` every package to a local wheelhouse
2. Creates a fresh venv
3. `pip install --no-index --find-links wheelhouse genblaze[all]`
4. `python -c "import genblaze_openai; import genblaze_google; ..."` smoke-test each connector

Runs as the **final gate** before publishing. ~50 lines of bash + a
single CI job.

---

## Tier 1 — must-fix

### B1 — Core version bump

**File:** `libs/core/pyproject.toml`
**Change:** `version = "0.2.8"` → `version = "0.3.0"`
**Why:** Prerequisite. Every other pin update resolves to a version
PyPI doesn't have until core is bumped.

### B3 — Connector version bumps

The hardening plan said "patch-level bump"; revised to **minor bump
to 0.3.0** because the connectors ship genuinely breaking API
changes (pricing tables removed, `register_pricing` semantics
changed, new ctor kwargs). Patch-level would misrepresent scope.

11 connectors → `version = "0.3.0"`:
- decart, elevenlabs, gmicloud, google, lmnt, luma, nvidia, openai,
  replicate, runway, stability-audio

Stay at current version (no code edits this release):
- langsmith, s3

### B2 — Pyproject pin sweep

**One coordinated edit across 15+ files** — direct deps, umbrella
extras, and the scaffolding skill template.

**11 active connectors** → `genblaze-core>=0.3.0,<0.4`:
- decart, elevenlabs, gmicloud, google, lmnt, luma, nvidia, openai,
  replicate, runway, stability-audio

**4 backward-compat packages** → widen ceiling only:
- langsmith: `>=0.2.0,<0.4`
- s3: `>=0.1.0,<0.4`
- cli: `>=0.2.0,<0.4`
- meta (`libs/meta`): `>=0.3.0,<0.4` (umbrella tracks newest stable)

**Umbrella extras** (`libs/meta/pyproject.toml` lines 39-87): every
connector pin in `[project.optional-dependencies]` (the `genblaze[video]`,
`[image]`, `[audio]`, `[all]` bundles) widens ceiling `<0.3` → `<0.4`.

**Skill template:** `.claude/skills/scaffold-provider/skill.md:56`
ships `>=0.2.0,<0.3` — bumps to `>=0.3.0,<0.4` so connectors scaffolded
after release land with correct pins.

### B4 — Umbrella in release pipeline

**File:** `.github/workflows/release.yml`
**Issue:** `publish-connectors` matrix omits `libs/meta`. The
`genblaze` umbrella package never gets published — users running
`pip install genblaze` after this release get the stale 0.2.x
umbrella with broken extras.

**Fix:** Add `publish-meta` job gated on `needs: publish-connectors`
(must run AFTER connectors so extras' pin targets exist on PyPI).

**Acknowledged partial-patch:** the matrix is hand-maintained; the
real root-cause is P3 (auto-discovered release matrix). Deferred to
2-week fast-follow.

### B5 — CHANGELOG cut

**File:** `CHANGELOG.md:8`
**Change:** `## [Unreleased]` → `## [0.3.0] - 2026-05-11`

Add a "Released package versions" subsection enumerating every package's
new version (matches the existing 0.2.9 entry pattern).

### B6 — `custom_model_registry.py` redesign (not patch)

**File:** `examples/custom_model_registry.py`

The current scenarios teach a 0.2.x mental model:
- S1 was teaching "unknown slugs hit fallback" — now mostly false because family patterns absorb most unknowns
- S2 was teaching "override shipped pricing" — now false because no pricing is shipped

**Redesign for 0.3.0 mental model:**

| Old scenario | New scenario |
|---|---|
| 1. Unknown model → fallback | **Register pricing on a family-matched slug** — demonstrates H4's family-aware fallthrough (preserves family contracts) |
| 2. Override pricing on dall-e-3 | **Register a new family for a vendor line the SDK doesn't ship** — demonstrates `register_family()` (the 0.3.0 power-user surface) |
| 3. Full custom spec | **Keep as-is** — still correct |

Patching scenarios 1 + 2 to handle the 0.3.0 reality would produce a
working-but-stale example. Redesign produces a forward-looking
onboarding asset.

### M1 — `0.3.0-rc1` release candidate first

Industry-standard for breaking-change releases. Tag `0.3.0-rc1`,
publish to PyPI, soak 48-72 hours with early adopters testing against
their real pipelines, then promote to `0.3.0` final.

**Rationale:** Rollback story for a breaking-change release of this size
is publishing `0.3.1` with corrections — high friction. RC catches
"actually used in production" issues that pre-release smoke can't.

---

## Tier 2 — release hygiene

| ID | What | Fix |
|---|---|---|
| S1 | NVIDIA missing from `.github/workflows/ci.yml` test matrix | Add to install + test loops |
| S2 | NVIDIA missing from `.github/workflows/release.yml` connector matrix | Add to publish matrix |
| S3 | `tools/check_pypi_metadata.py --strict` will fail on missing `Changelog` URLs across most connectors | Batch-add `Changelog` to each `[project.urls]` |
| S4 | `examples/ingest_ugc_upload.py:68-72` soft drift on `file://` Asset URLs | Add one-line comment noting production apps should use https:// not file:// |
| S5 | `CLAUDE.md` `last_verified: 2026-04-22` predates this PR | Bump to 2026-05-11 |
| S6 | CHANGELOG `[Unreleased]` could be re-introduced accidentally on next release | Add ~10-line `grep` gate to `release.yml` — fails if `[Unreleased]` still present |

---

## Tier 3 — deferred (open as fast-follow issues)

| ID | What | Why deferred |
|---|---|---|
| P1 | `tools/check_version_consistency.py` CI check — asserts all connector core-pin ceilings match `<{next_minor_of_core}` | Would have prevented B2 entirely; structural fix, not release-blocking |
| P3 | Auto-discovered release matrix (glob `libs/**/pyproject.toml`) | Replaces hand-maintained matrix; eliminates B4-class gaps for future packages. **Escalated to "within 2 weeks of 0.3.0 ship."** |
| P4 | Beyond-NVIDIA: every connector in `Makefile` must be in `ci.yml` and `release.yml` | Documented principle, enforced by a lint check on workflow yaml |

---

## Execution order

Tier 0 + Tier 1 must land in one PR (release-mechanics changes are
atomic). Order within the PR:

1. **B1** (single-line core version) — prerequisite
2. **B3** (11 connector versions) — batch edit
3. **B2** (pin sweep) — depends on B1+B3 being settled so the targets exist
4. **B4** (release.yml umbrella job)
5. **B5** (CHANGELOG cut)
6. **B6** (custom_model_registry redesign)
7. **S1-S6** (release hygiene, any order)
8. **M3** (TS types verify — pre-merge check)
9. **M2** (wheel smoke test — added to CI, run pre-tag)

Post-merge:
10. **M1** (tag `0.3.0-rc1`, soak 72hr, then `0.3.0` final)

---

## Acceptance criteria

1. `make lint && make typecheck` clean
2. `make test` clean across core + every connector + cli
3. **`make release-smoke` green** (new — Tier 0)
4. `make ts-types` produces no diff
5. Every `pyproject.toml` lists a version compatible with `genblaze-core 0.3.0`
6. `libs/meta` extras pin every connector at `>=0.X.x,<0.4`
7. `examples/custom_model_registry.py` runs to completion (all 3 scenarios)
8. CHANGELOG has `## [0.3.0] - 2026-05-11` block with per-package version list
9. `release.yml` includes both NVIDIA and meta in publish matrix
10. `0.3.0-rc1` tag soaked 72hr before `0.3.0` final tag

---

## Risks

| Risk | Mitigation |
|---|---|
| Connector version bump breaks downstream pipelines pinning old versions | Documented in migration guide's Deprecation Horizon section; RC soak surfaces issues before final |
| Wheel smoke test catches a packaging regression late in the release | Acceptable — better than catching it post-publish |
| Hand-maintained release matrix drifts again | P3 (auto-discovery) is the structural fix; tracked as fast-follow |
| Time pressure tempts skipping RC | Don't skip RC. The 72hr soak is non-negotiable for breaking-change releases |
