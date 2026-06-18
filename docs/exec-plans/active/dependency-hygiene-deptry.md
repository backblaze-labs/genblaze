<!-- created: 2026-06-18 -->
# Dependency Hygiene — deptry gate + declared imports

Single PR closing the class of bug found in #37/#106 (`genblaze-replicate` imported
`httpx` on its default path without declaring it, crashing a clean install). A repo-wide
`deptry` scan found the remaining offenders. Two are the *same severity* as replicate —
unguarded imports of undeclared deps reachable on a clean install:

- `genblaze-core` imports `urllib3` unguarded at module load in `storage/transfer.py`
  (the `PoolManager` behind `AssetTransfer`), and `storage/__init__` imports it eagerly,
  so `import genblaze_core.storage` crashed a `pydantic`+`pillow`-only install.
- `genblaze-core/testing.py` (the public `MockProvider` / `ProviderComplianceTests`
  harness, shipped in the wheel and documented in connector READMEs) imports `pytest` at
  module load, which was declared dev-only — so `import genblaze_core.testing` crashed too.

The rest are guarded soft deps (advertised as extras) or genuine cross-package classifiers
(ignored). So this is a correctness + tooling pass that also fixes two latent clean-install
crashes.

Goal: declare what we import, advertise soft integrations as extras, and add a CI gate so
the next undeclared import fails locally instead of reaching a user.

`deptry` only closes the *undeclared-import* direction (we import `httpx` without declaring
it). The complementary direction — *declared-but-broken version constraints* (a `>=` floor
that's never installed against, or two packages' pins that can't co-resolve) — is out of
deptry's reach and is covered in the [Follow-up section](#follow-up--version-resolution-gates-separate-pr).
That direction is not academic here: this PR *adds* new `>=` floors (`httpx>=0.24`,
`opentelemetry-api>=1.20`, `botocore>=1.31`, `aiobotocore>=2.7`), and CI today installs with
`pip install -e`, which ignores version constraints entirely — so nothing proves those floors
actually resolve until release-time `make release-smoke`.

## Goals & success criteria

- **No undeclared default-path imports** anywhere in the workspace.
- **`make deptry` passes** for every package and is wired into `make lint` and `make pre-release`.
- **No behavior change, no new public API** — packaging metadata + config only, so this
  ships as a patch wave.
- **`make pypi-pin-parity` stays green** — every package whose metadata changes gets a
  version bump in the same PR.

## Findings (deptry 0.25.1, run per-package 2026-06-18 — verified during implementation)

| Package | Import | Rule | Reality | Action |
|---|---|---|---|---|
| `genblaze-core` | `urllib3` (storage/transfer.py) | DEP003 | **Unguarded module-load import, eager via `storage/__init__`**; not transitively guaranteed (core deps = pydantic+pillow) | **Declare** `urllib3>=1.26,<3` in deps |
| `genblaze-core` | `pytest` (testing.py) | DEP004 | Shipped public test harness imports pytest at module load; was dev-only | **Add `testing` extra** (`pytest>=7.0`) |
| `genblaze-core` | `httpx` (providers/retry.py) | DEP003 | Guarded soft dep | Add `http` extra (declared → resolves) |
| `genblaze-core` | `opentelemetry` (observability, storage) | DEP003 | Guarded soft dep | Add `otel` extra + `package_module_name_map` (`opentelemetry-api` → `opentelemetry`) |
| `genblaze-core` | `botocore` (storage/errors.py) | DEP003 | Cross-package classifier; reached only when a connector already loaded botocore | `per_rule_ignores` DEP001+DEP003 (do **not** add to core deps) |
| `genblaze-core` | `google` (providers/pattern_safety.py) | DEP003 | Optional `google-re2` accelerator, guarded | `per_rule_ignores` DEP001+DEP003 |
| `genblaze-s3` | `botocore` (backend.py) | DEP003 | Unguarded; always ships with declared `boto3` | Declare `botocore>=1.31` in deps |
| `genblaze-s3` | `aiobotocore` (async_backend.py) | DEP001 | Guarded; always ships with declared `aioboto3` (`async` extra) | Declare `aiobotocore>=2.7` in `async` extra |
| **all 16 packages** | dev tools (`pytest`, `ruff`, `mypy`, …) | DEP002 | Test/dev tooling, not imported in shipped code | `[tool.deptry] optional_dependencies_dev_groups = ["dev"]` per package |
| `libs/meta` | all 14 `genblaze-*` deps | DEP002 | Umbrella metapackage — deps are install-time bundles, not imports | **Exclude meta** from the gate |

Notes that corrected the original table: the `google` DEP003 is in **core** (`google-re2`), not a
false-positive in the google connector (which is clean apart from dev-tool noise); `urllib3` is a
hard dep, not an ignorable transitive; DEP002 dev-tool noise and the `testing.py` DEP004 were not
anticipated. `cli`'s `pillow` flag is resolved by the dev-group config (it lives in `cli[dev]`).

## Scope — one PR, one branch (`chore/dependency-hygiene-deptry`)

### 1. Add the deptry gate (prevents recurrence — the point of the PR) — done
- Added `deptry>=0.23` to `core[dev]` (the canonical dev-tool location, alongside ruff/mypy);
  `make install-dev` propagates it. Floor is `0.23` because the gate uses the modern
  `optional_dependencies_dev_groups` config key (the older `pep621_dev_dependency_groups` is
  deprecated in 0.25).
- Added a `deptry` target to the `Makefile` (per-package loop, **excludes `libs/meta`**),
  folded into `make lint` (so `make pre-release` inherits it via `lint`).
- Documented it in `RELEASING.md` alongside the existing gates.
- Added a dedicated **`deptry` CI job** that runs `make install-dev`, then `pip check`
  (installed-set conflict/missing-dep gate — the cheap counterpart to deptry), then
  `make deptry`. A standalone job mirrors the repo's one-job-per-concern convention; the
  editable install means deptry can map imports to packages. The stronger constraint-
  respecting wheel install is the follow-up below.
- Per-package `[tool.deptry] optional_dependencies_dev_groups = ["dev"]` added to all 14
  checked packages (core, s3, the 12 other connectors, cli) so dev tooling isn't flagged
  DEP002. `libs/meta` is excluded from the gate entirely.

### 2. `genblaze-s3` — declare direct imports (bump 0.3.2 → 0.3.3) — done
- `dependencies`: added `botocore>=1.31` (tracks the `boto3>=1.28` floor; boto3 pins the
  exact version, so no resolver conflict).
- `async` extra: added `aiobotocore>=2.7` (`aioboto3` pins the exact version).
- CHANGELOG entry added.

### 3. `genblaze-core` — declare urllib3, advertise soft deps as extras (bump 0.3.2 → 0.3.3) — done
- **Declared `urllib3>=1.26,<3` as a hard runtime dependency** (unguarded module-load import —
  see findings). This is the substantive fix, not an ignore.
- Added extras: `http = ["httpx>=0.24"]`, `otel = ["opentelemetry-api>=1.20"]`,
  `testing = ["pytest>=7.0"]` (the shipped `genblaze_core.testing` harness). Added `httpx`
  and `opentelemetry-api` to `dev` so the guarded paths run under test.
- `[tool.deptry]`: `optional_dependencies_dev_groups = ["dev"]`;
  `per_rule_ignores` DEP001+DEP003 = `["botocore", "google"]` (cross-package classifier +
  optional `google-re2`); `package_module_name_map` mapping `opentelemetry-api` →
  `opentelemetry` so the `otel` extra is recognized without the package installed.
- CHANGELOG entry added.

### 4. Other connectors + cli — deptry config only (no version bump) — done
- Added `[tool.deptry] optional_dependencies_dev_groups = ["dev"]` to all 12 remaining
  connectors and `cli`. The google connector needed **no** module map — deptry 0.25 resolves
  `google.genai` to the declared `google-genai` correctly; only the dev-tool DEP002 applied.
- No `Requires-Dist` change → no version bump (tool tables aren't distribution metadata),
  so `make pypi-pin-parity` stays green for these.

## Decisions

- **`urllib3`: declared (not ignored).** The original table marked it an ignorable transitive;
  verification showed it's an unguarded module-load import reachable on a clean install — the
  same class as #37/#106. Declaring it is the honest, minimal fix (boto3 already needs urllib3,
  so no resolver conflict). Guarding the import + making it an extra was rejected: `AssetTransfer`
  is core's byte-transfer path, not optional, and that would be a behavior change.
- **`testing.py` pytest: `testing` extra (not a code change).** The harness is a documented
  public surface; advertising the extra fixes the clean-install crash without touching code.
- **s3 botocore/aiobotocore: declared** (the original plan's lean). boto3/aioboto3 pin the exact
  versions, so the declarations add honesty for the gate with zero resolver effect.
- **`libs/meta` excluded from the gate.** An umbrella metapackage's deps are install-time bundles,
  not imports; deptry's import-vs-declaration model doesn't apply, and a blanket DEP002 ignore
  would need editing on every new connector.
- **Version wave.** Bundles `genblaze-core 0.3.3` + `genblaze-s3 0.3.3` (independent of the wave
  tag, which follows the CHANGELOG header). Other packages unchanged.

## Test plan

- [x] `make deptry` passes for all 16 checked packages (meta excluded) — verified
- [x] `make pypi-pin-parity` green (core + s3 at new unpublished 0.3.3 → skipped; 0 drift)
- [x] `pip check` reports no genblaze-* conflicts after a consistent `make install-dev`
- [x] `make test` passes (core 1577, s3 246, meta 8; connectors unaffected — metadata-only)
- [x] `make lint` passes (now includes deptry)
- [x] CHANGELOG updated for core + s3 + tooling
- [x] Smoke: `import genblaze_core.storage` and `import genblaze_core.testing` succeed

## Code review follow-through (4-dimension EM review)

A parallel EM review (architecture, security/reliability, B2, test coverage) ran against the
diff. Outcomes:

- **False positive (security, "Critical"):** claimed `urllib3>=1.26` conflicts with botocore's
  `urllib3<1.27` cap at minimum floors. Verified empirically — pip backtracks to `urllib3
  1.26.20` (satisfies both); the realistic set resolves to `urllib3 2.0.7`. No conflict.
- **Applied — floor accuracy:** `aiobotocore>=2.5` → `>=2.7` (aioboto3 12.0.0 pins
  `aiobotocore==2.7.0`, the true minimum); `urllib3>=1.26` → `>=1.26,<3` (repo bounded-major
  convention, cf. `pydantic<3`).
- **Applied — test coverage gap (the strongest real finding):** `release_smoke.sh`,
  `install-verify`, and `post-release` only `import genblaze_core`, whose lazy `__getattr__`
  never executes `storage/transfer.py` — so they would NOT catch a urllib3-removal regression.
  Added `genblaze_core.storage` to the `release_smoke.sh` import list (urllib3 is now a hard
  dep, present in any core install). Deliberately did NOT add `genblaze_core.testing` there —
  `genblaze[all]` does not pull `genblaze-core[testing]`, so pytest is absent in that venv;
  bare-venv validation of the `testing` extra is routed to the entry-point follow-up below.
- **Applied — maintainability:** `make deptry` now globs `libs/connectors/*/` (auto-includes
  new connectors, can't silently skip one); CI `lint` and `deptry` jobs gained comments
  explaining the ruff-vs-`make lint` split and the editable-install limitation of `pip check`.
- **Applied (after maintainer authorization):** `.claude/skills/scaffold-provider/SKILL.md`
  now lists `[tool.deptry] optional_dependencies_dev_groups = ["dev"]` as a required
  `pyproject.toml` field, so a newly scaffolded connector won't fail `make deptry`. The
  Makefile glob means no Makefile edit is needed for new connectors — only the per-package
  config.

## Follow-up — version-resolution gates (separate PR)

Out of scope for the deptry PR (new CI infra, not packaging metadata), but the natural next
step and motivated by the `>=` floors this PR introduces. Sourced from deep-research on how
LangChain and the broader Python ecosystem catch cross-package dependency conflicts; see the
research report in the conversation thread for citations. Sequenced by payoff:

1. **Constraint-respecting install + `pip check` on every PR (highest payoff).** CI's
   `pip install -e` bypasses version constraints, so a bad pin only surfaces at release time
   via `make release-smoke`. Add a `resolve-check` job that installs the *built wheels*
   (`genblaze[all]` from the wheelhouse, non-editable) and runs `pip check`. This is
   `release_smoke.sh` running per-PR instead of pre-tag — the hard part is already written, so
   the job can largely call `make release-smoke`.

2. **Minimum-version job — validates the `>=` floors this PR adds.** LangChain's central
   technique: install with `uv pip install --resolution lowest-direct` and run `make test`
   against the declared floors. Without it, `httpx>=0.24` / `botocore>=1.31` / `aiobotocore>=2.7`
   / `opentelemetry-api>=1.20` are untested assertions. Watch the lockfile trap: if a uv
   lockfile is ever committed, `lowest-direct` resolves against it and masks too-low bounds
   (FastMCP #2290) — bypass the lockfile for this job, or use `tox-uv`'s `uv_resolution`.

3. **Entry-point validation.** `release_smoke.sh` already asserts each connector *imports*;
   strengthen it to load providers via `importlib.metadata.entry_points()` so the registration
   mechanism (not just the module) is exercised.

4. **Tooling hygiene (no migration).** Keep `check_pin_parity.py` as-is — it guards the
   release `skip-existing` silent-skip trap the above gates don't cover. If grouped dependency
   bumps are wanted, configure Renovate; do *not* switch off Dependabot on the belief it can't
   group monorepo bumps — it can via `groups` config (the research's one refuted claim).

Caveat on a future uv migration: a single uv workspace shares one lockfile, which assumes all
members agree on versions. Providers that need divergent transitive pins (the `httpx`
situation is the canary) must be isolated via path dependencies rather than forced into one
workspace lockfile — which is exactly where per-package `pip check` and min-version jobs earn
their keep.
