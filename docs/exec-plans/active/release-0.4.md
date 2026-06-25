<!-- last_verified: 2026-06-25 -->
# Release 0.4.0 — readiness sweep

> Security hardening wave (SSRF/URL-only asset verification), two new
> providers (Hume Octave TTS, AssemblyAI STT), connector floor refresh to
> `genblaze-core>=0.3.4`, and a mutagen cap to close a future CI-break
> vector. The code is merged (PRs #77, #108–#116). This plan closes the
> remaining release-mechanics gap: the `genblaze` umbrella was already
> published at 0.4.0, blocking all changed pins from republishing.

## Why this plan exists

The umbrella `genblaze==0.4.0` was published during an earlier prep window
before the connector floor updates and new provider extras were finalised.
PyPI does not allow overwriting an existing version, so:

- `pip install "genblaze[hume]"` resolved against the 0.4.0 wheel, which had
  `genblaze-hume>=0.3.0` — below the 0.3.1 release this wave ships.
- `make release-smoke` stayed red because the smoke venv built from the local
  wheelhouse produced a `genblaze==0.4.0` wheel with corrected extras, but
  the pin-parity gate compared against the *published* 0.4.0 wheel and
  reported drift across every connector extra.
- The only fix is advancing the umbrella to `0.4.1` so a new, corrected wheel
  can publish.

Three additional housekeeping items are bundled: capping mutagen to prevent a
repeat of the #108 breakage, and cutting the CHANGELOG wave header.

## Scope

| Tier | Items | Why |
|---|---|---|
| 0 (gate) | `make pre-release` — lint, deptry, mypy, version-coherence, pin-parity, full test suite, release-smoke | Must be fully green before tagging |
| 1 (must-fix) | Umbrella version bump, mutagen cap, CHANGELOG cut | Without these, release-smoke is red / CI can re-break |
| 2 (hygiene) | Inline pyproject comments, CHANGELOG prose accuracy | Discoverability; no functional impact |

Explicitly out of scope: connector logic, test changes, new features,
workflow edits.

---

## Tier 0 — pre-release gate

### G1 — `make pre-release` end-to-end

Must pass all sub-gates before the tag is created:

1. `make lint` — ruff check + format
2. `make deptry` — undeclared / misclassified deps across all 17 packages
3. `make typecheck` — mypy on `libs/core/genblaze_core/`
4. `make pypi-metadata-check` — version coherence across all pyproject.toml files
5. `make pypi-pin-parity` — compares each package's source pins against the
   published PyPI wheel; 0 drift required (18 fresh packages expected this wave)
6. `make test` — full suite across core + CLI + all connectors + tools/tests
7. `make release-smoke` — builds local wheelhouse, installs `genblaze[all]`
   into a fresh venv, asserts every connector imports cleanly

---

## Tier 1 — must-fix

### B1 — Umbrella version bump (the gating fix)

**File:** `libs/meta/pyproject.toml`
**Change:** `version = "0.4.0"` → `version = "0.4.1"`
**Why:** PyPI cannot overwrite an existing version. The 0.4.0 wheel has stale
extras floors (e.g. `genblaze-hume>=0.3.0` instead of `>=0.3.1`) and does
not include the `assemblyai` extra at all. Bumping to 0.4.1 lets a corrected
wheel publish and unblocks `release-smoke`.

**Note on naming:** The CHANGELOG wave header and the GitHub Release tag are
both `v0.4.0` (the wave name). The umbrella PyPI package publishes as `0.4.1`
(the corrected wheel). This split is intentional — the tag records what wave
shipped, the version records the wheel on PyPI. Do not rename the tag to
`v0.4.1`; do not revert the pyproject to `0.4.0`.

### B2 — Mutagen cap in `genblaze-core`

**File:** `libs/core/pyproject.toml`
**Change:** `mutagen>=1.47` → `mutagen>=1.47,<1.49` in BOTH `[audio]` extra
and `dev` extra.
**Why:** Mutagen 1.48 changed m4a container timescale handling in a way that
broke `test_aac_handler.py`. The fixture was patched in #108, but the
uncapped floor means a future mutagen release can reintroduce the same break
before CI catches it. Capping at `<1.49` permits 1.47 and 1.48 (vetted) and
guards against 1.49+ until it is explicitly tested. Revisit when 1.49 ships.

Both extras are capped independently — the comment is duplicated, matching
the existing pattern in this file (cf. urllib3, httpx inline comments).

### B3 — CHANGELOG wave header

**File:** `CHANGELOG.md`
**Change:** `## [Unreleased]` → `## [0.4.0] - 2026-06-25` with a fresh empty
`## [Unreleased]` scaffold above it.

The `[0.4.0]` block must open with a `### Released package versions`
subsection (per RELEASING.md step 2 and every prior wave back to [0.2.2]).

---

## Per-package version table

Authoritative old versions are from PyPI LATEST at time of tagging.

| Package | PyPI before | This wave | Notes |
|---|---|---|---|
| `genblaze` (umbrella) | 0.4.0 | **0.4.1** | Patch republish; extras/floors corrected |
| `genblaze-core` | 0.3.2 | **0.3.4** | SSRF hardening, URL-only verify, sink lifecycle, fan-in, async preflight, error sanitiser, tenant cache |
| `genblaze-s3` | 0.3.2 | **0.3.4** | Core floor bump; `S3StorageBackend.close()` shuts boto3 client |
| `genblaze-cli` | 0.3.0 | **0.3.2** | Core floor bump; exposes `verify_hash()` and sha256 diagnostics |
| `genblaze-replicate` | 0.3.0 | **0.3.2** | Core floor bump |
| `genblaze-gmicloud` | 0.3.1 | **0.3.2** | Core floor bump |
| `genblaze-hume` | — | **0.3.1** (new) | Hume Octave TTS provider |
| `genblaze-assemblyai` | — | **0.3.0** (new) | AssemblyAI speech-to-text provider |
| `genblaze-openai` | 0.3.0 | **0.3.1** | DALL-E URL outputs materialised locally; pinned-DNS download; core floor bump |
| `genblaze-google` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-decart` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-elevenlabs` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-langsmith` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-lmnt` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-luma` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-nvidia` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-runway` | 0.3.0 | **0.3.1** | Core floor bump |
| `genblaze-stability-audio` | 0.3.0 | **0.3.1** | Core floor bump |

---

## Tier 2 — release hygiene

| ID | What | Fix |
|---|---|---|
| S1 | Mutagen cap has no inline comment — zero discoverability | Add one-line comment in both `audio` and `dev` extras referencing #108 and 1.48 behaviour change |
| S2 | CHANGELOG `[0.4.0]` block missing `### Released package versions` subsection | Required by RELEASING.md step 2; every prior wave has it |

---

## Execution order

All changes land in one PR (release-mechanics changes are atomic).

1. **B1** (umbrella version bump) — unblocks release-smoke
2. **B2** (mutagen cap) — rides core 0.3.4, no additional core version bump
3. **B3** (CHANGELOG cut with `### Released package versions` table)
4. **S1** (inline pyproject comments)
5. **G1** (`make pre-release`) — must be fully green before pushing

Post-merge release sequence:

6. Create GitHub Release with tag `v0.4.0` — triggers `.github/workflows/release.yml`
7. Workflow publishes `publish-core` first, then `publish-cli` and
   `publish-connectors` in parallel, then `publish-meta` last
8. `make post-release VERSION=0.4.1` — verifies `pip install "genblaze[all]==0.4.1"`
   resolves from public PyPI

---

## Acceptance criteria

1. `make lint && make typecheck` clean
2. `make test` clean across core, CLI, all connectors, tools/tests
3. **`make release-smoke` green** — `genblaze==0.4.1` wheel installs `genblaze[all]`; all 18 connector imports pass
4. `make pypi-pin-parity` reports `0 drift, 18 fresh`
5. `libs/meta/pyproject.toml` at `version = "0.4.1"`
6. `libs/core/pyproject.toml` mutagen pin is `>=1.47,<1.49` in both `[audio]` and `dev` extras
7. `CHANGELOG.md` has `## [0.4.0] - 2026-06-25` with `### Released package versions` subsection
8. GitHub Release tag is `v0.4.0`; `make post-release VERSION=0.4.1` passes

---

## Risks

| Risk | Mitigation |
|---|---|
| Tag name (`v0.4.0`) does not match umbrella PyPI version (`0.4.1`) — ops confusion | Document the split explicitly; `post-release` uses `VERSION=0.4.1`; RELEASING.md covers the umbrella-version convention |
| Mutagen 1.49 ships before we revisit the cap | `<1.49` holds CI green; revisit by running the audio test suite against 1.49 in a branch before widening |
| `publish-meta` job runs before a connector wheel is available on PyPI | `publish-meta` is gated on `needs: publish-connectors` in release.yml; umbrella extras can only resolve once connector wheels exist |
