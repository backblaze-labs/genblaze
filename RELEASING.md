# Releasing Genblaze

How releases are cut and how the automated publish pipeline works.

## Versioning policy

Genblaze ships ~15 PyPI packages and one npm package from this monorepo.
Each package's `pyproject.toml` carries its own version number, and they
do **not** all move in lockstep — e.g. the 0.3.0 wave bumped `genblaze-core`
and every connector to `0.3.0`, but left `genblaze-s3` and `genblaze-cli`
unchanged because their code hadn't shifted. In the same wave the umbrella
`genblaze` package moved from `0.3.2` → `0.4.0` (it had its own version
history that ran ahead of core), so the wave is called "0.3.0" but the
umbrella package version is `0.4.0`. That's expected, not a bug.

We call each release a **wave**. The canonical name of the wave is the
latest version file in `.changes/`, rendered into `CHANGELOG.md`. The GitHub Release tag
follows the wave name — _not_ any individual package's version — because:

* No single package is bumped on every wave (a connectors-only wave
  wouldn't touch `core`; a core-only wave wouldn't touch every connector).
* The CHANGELOG is the only file that always exists for every wave and
  always reflects the maintainer's stated release name.
* Per-package PyPI versions are still whatever each `pyproject.toml`
  says — the workflow publishes them as-is.

Convention:

| Surface                          | Source of truth                                    |
| -------------------------------- | -------------------------------------------------- |
| GitHub Release tag               | `v` + latest `## [X.Y.Z] - …` heading in `CHANGELOG.md` |
| Per-package PyPI versions        | Each package's own `pyproject.toml`                |
| `@genblaze/spec` version (npm)   | `libs/spec/package.json`                           |
| What changed                     | Changie fragments → `.changes/vX.Y.Z.md` → `CHANGELOG.md` |

The release workflow validates `tag == "v" + latest CHANGELOG wave name`
before doing anything else. A mismatch fails the run.

## Changie notes and wave names

Changie v1.26.0 is pinned in the Makefile. Go downloads it with checksum
verification; an installed binary of that version can be selected with
`CHANGIE=/path/to/changie`. Fragments are grouped by package, while a wave is
still an explicitly selected `X.Y.Z`. There are no Changie replacements that
rewrite package versions. Historical wave text was migrated (with one legacy trailing space removed) into
`.changes/vX.Y.Z.md` files. The legacy PEP 440 `0.2.5.post1` section is
retained after `0.2.6` in that archive file because Changie only sorts SemVer
filenames; its original heading, contents and position remain unchanged.

During development:

```bash
make changelog-new          # component, kind, and user-facing description
make changelog-merge        # refresh after manually editing a fragment
make changelog-check        # reject hand edits / stale generated output
```

At the release freeze, after choosing the wave and applying the package-version
plan with `tools/prepare_release.py`:

```bash
make changelog-batch WAVE=X.Y.Z
make changelog-release-check
```

Batching creates `.changes/vX.Y.Z.md` with an exact package-version inventory,
consumes the pending fragments, and regenerates CHANGELOG.md. It never bumps
package versions. Use that version file as the GitHub Release body:

```bash
gh release create vX.Y.Z --verify-tag --title X.Y.Z --notes-file .changes/vX.Y.Z.md
```

Close the matching milestone only after every issue and PR assigned to it has
landed or been moved to a later wave. A missing/open milestone or any open
issue/PR blocks production in `validate-version`. Historical unsigned tags and
missing historical milestones are not retroactively rewritten.

## Signing trust

`.github/release-signers` contains approved SSH public signing keys. Its initial
entry is the code owner's published signing key (GitHub key id 1054051). Review
its fingerprint with the owner before merging this trust bootstrap. Additions,
rotation, and revocation require CODEOWNERS review on protected `main`; never
fetch a key from a keyserver or trust an allowlist supplied only by a tag.

Production executes `git verify-tag` using the allowlist read from `origin/main`,
requires the tag commit on that branch, and pins every downstream checkout to
that verified commit. Use `python tools/release_gate.py --tag vX.Y.Z` locally
with GitHub read access to check both the signature and milestone. Signed,
unsigned, lightweight, unknown-key and off-main tags have regression tests.

## Trigger decision pending human review

The current trigger remains **GitHub Release published → gates → publish →
install verification**. A failed publish can therefore leave a visible Release
whose packages are absent. GitHub Release publication is a request to run the
pipeline, not proof of its completion.

The fleet standard is **signed tag → gates → publish → install verification →
GitHub Release**. Recommend adopting it in a separately approved change: it
makes the Release a completion signal and avoids announcing a failed publish.
That change must include tag rulesets, release-note creation permissions and
partial-publication recovery. A human can instead approve the current ordering
as an exception, protected by signed tags, closed milestones, the four gates,
protected publishing environments and post-publish verification. This PR does
not record that exception as approved and does not change the event trigger.

## Dependency pinning policy

Two independent conventions apply to every `pyproject.toml`'s
`[project.dependencies]` and product-facing `optional-dependencies` extras
(dev/test tooling extras like `dev`/`testing` are exempt — see below):

* **Internal `genblaze-*` deps** are pinned `>=<current>,<0.4` (bump the
  upper bound only when the monorepo's own version line advances — see
  `tools/check_pin_parity.py`).
* **Third-party runtime deps** are capped below their next major version
  (`>=X.Y,<X+1`), including packages still on a pre-1.0 line (`>=0.Y,<1`) —
  e.g. `pydantic>=2.0,<3`, `lmnt>=2.6,<3`, `elevenlabs>=2.0,<3`,
  `assemblyai>=0.45,<1`, `hume>=0.13.13,<1`. One rule for both pre- and
  post-1.0 SDKs keeps the policy mechanical instead of requiring a judgment
  call per package about how "volatile" its 0.x line is. An uncapped
  third-party dep lets a vendor's major release break a clean `pip install`
  at resolve time with no code change on our side — this is the exact class
  of bug that broke `genblaze-lmnt` and `genblaze-elevenlabs` (#166, #163).
  Dev/test tooling extras (`pytest`, `ruff`, `mypy`, `deptry`, `hypothesis`,
  `jsonschema`, etc., normally grouped under a `dev` or `testing` extra) are
  exempt from this cap — they're never resolved into a production install,
  so a future major there costs us a CI failure to fix, not a broken
  consumer install.

  When a connector's floor is already several majors behind the version it
  actually resolves to today (a floor-staleness bug in its own right, not a
  packaging-cap issue), cap one major past the version currently verified
  to work rather than the floor's next major — capping tighter than what's
  actually in use would make a fresh install worse, not safer.

## Pre-release checklist

Before cutting a release, verify on `main`:

1. **Versions are bumped.** Every package whose code changed since the
   last release has its `pyproject.toml` `version` updated. The umbrella
   `libs/meta/pyproject.toml` is bumped to reflect this release, along with
   its (and `cli`'s) `genblaze-core`/`genblaze-s3`/connector-extra floors.
   `python3 tools/prepare_release.py --check`/`--apply` automates this
   step deterministically — see `.claude/skills/prepare-release/SKILL.md`
   for the end-to-end driver.
2. **Changie notes are batched.** Follow the commands below. No YAML fragments
   remain in `.changes/unreleased/`, and `make changelog-release-check` passes.
   The generated wave header lists exact package versions; `prepare_release.py`
   still controls version bumps and dependency floors.
3. **TS types are current.** `make ts-types` produces no diff. (CI's
   `ts-types-check` job already enforces this on every push, but the
   release workflow re-runs it as a defense in depth.)
4. **CI is green** on the commit you're about to release.
5. **Milestone is closed.** The wave's exact `vX.Y.Z` milestone exists, is
   closed, and has no open issues or PRs. Assign related issues and PRs when
   planning; move deferred work before closing the milestone. Choose the next
   wave explicitly rather than inferring it from a package version.
6. **Tag is signed.** Create a signed annotated SSH tag with an approved
   maintainer key. Production verifies it against `.github/release-signers`
   from protected `main`, and rejects commits outside `main`.

Step 3 plus the local equivalents of every gate the publish pipeline
will run are bundled into a single target:

```bash
make pre-release
```

This runs `lint`, `typecheck`, `ts-types-check`, `pypi-metadata-check`,
`pypi-pin-parity`, `test`, and `release-smoke` in quick-fail order. `lint`
now also runs the `deptry` dependency-hygiene gate (`make deptry`), which
fails if any package imports a dependency it doesn't declare — the
clean-install crash class behind #37/#106. A clean run on `main` is a strong
signal that `validate-version`, `changelog-gate`, and `release-smoke` in the
workflow will all be green once you tag.

`release-smoke` runs `twine check`, and CI installs `twine` unpinned. Keep
your local `twine` and `packaging` current (`pip install -U twine packaging`);
older versions reject the wheels' metadata version (2.5) with `InvalidDistribution`.

## The publish pipeline

Defined in [`.github/workflows/release.yml`](.github/workflows/release.yml).
Triggered two ways:

### 1. Production release (`release: published`)

1. Manually create a signed annotated Git tag matching the latest CHANGELOG
   wave name:
   ```bash
   # If CHANGELOG's most recent versioned heading is `## [0.3.0] - …`:
   git -c gpg.format=ssh -c user.signingkey=/path/to/signing-key tag -s v0.3.0 -m "Release 0.3.0"
   git push origin v0.3.0
   ```
2. Create a GitHub Release on that tag. Paste the CHANGELOG slice for
   this version as the body — **do not** use `gh release create --notes-from-tag`;
   it repeats the annotated tag's one-line message (`Release 0.3.0`) instead of
   the CHANGELOG content, which is how v0.5.0 and v0.6.0 originally shipped
   with just the tag message as their entire body (#250). Extract the slice
   and pass it via `--notes-file`:
   ```bash
   awk '/^## \[0.3.0\]/{p=1} p&&/^## \[/&&!/^## \[0.3.0\]/{exit} p' CHANGELOG.md \
     > /tmp/release-notes-0.3.0.md
   if test -s /tmp/release-notes-0.3.0.md; then
     gh release create v0.3.0 --title "0.3.0" --notes-file /tmp/release-notes-0.3.0.md
   else
     echo "empty slice — check the wave heading; release NOT created" >&2
   fi
   ```
3. Publishing the Release fires the `Release` workflow.

The workflow then runs:

```
validate-version ─┐                              ┌──> publish-cli
                  ├──> publish-core ─────────────┤
changelog-gate ───┤                              └──> publish-connectors (matrix×13) ──> publish-meta ─┐
                  │                                                                                    ├──> install-verify
release-smoke ────┤                                                                                    │
                  │                                                                                    │
pin-parity ───────┘                                                                                    │
                                                                                                       │
publish-npm ───────────────────────────────────────────────────────────────────────────────────────────┘
```

Notes on the graph:

* `validate-version`, `changelog-gate`, `release-smoke`, and
  `pin-parity` are the four pre-publish gates. All must pass before
  any package is built.
* `publish-cli` runs in parallel with the connector matrix but is NOT
  on the path to `install-verify`. A `publish-cli` failure marks the
  overall run red but doesn't cancel verify — verify is a smoke test
  for the umbrella import, not a full release-wide check.
* `publish-npm` runs in parallel with the entire PyPI graph (no PyPI
  dep), but `install-verify` waits for both `publish-meta` and
  `publish-npm` before declaring the wave released.

* **`validate-version`** — reads the canonical wave name from the
  latest version file in `.changes/`, rendered into `CHANGELOG.md`, enforces `tag ==
  "v$wave"` on `release` trigger, and emits the wave name +
  per-package versions + `dry_run` flag to every downstream job.
* **`changelog-gate`** — fails if `[Unreleased]` still has entries.
* **`release-smoke`** — runs `make release-smoke`: builds every wheel,
  installs local genblaze wheels while leaving public PyPI enabled for
  transitive dependencies, then imports every connector. Catches version-pin
  mismatches before PyPI sees them.
* **`pin-parity`** — for every package, compares source
  `[project.dependencies]` **and** `[project.optional-dependencies]`
  against the wheel already on PyPI at the same version. Fails the
  release if either base deps or any extra group diverges. Closes the
  `skip-existing` trap that shipped twice (s3 in 0.3.0, langsmith +
  cli in 0.3.2): without this gate, a package whose pin was widened
  in source but whose version was never bumped will be silently
  skipped on every release, leaving the broken wheel resolvable. This
  is especially important for the `genblaze` umbrella, whose
  connector pins and `video`/`image`/`audio`/`all` bundles all live
  under `[project.optional-dependencies]`. **Every** extra the wheel
  ships is compared — including tooling extras like `dev`/`testing` —
  since a stale pin in any published extra is still silently kept by
  `skip-existing`. Extra names match case- and separator-insensitively
  (PEP 685), so `stability-audio` and `stability_audio` are one extra.
  Run locally via `make pypi-pin-parity` (also bundled into
  `make pre-release`).
* **`publish-core`** — must publish first; everything else pins it.
* **`publish-cli` + `publish-connectors`** — fan out in parallel. The
  connectors matrix uses `fail-fast: false` so one transient PyPI hiccup
  doesn't strand the others.
* **`publish-meta`** — last, because the umbrella's
  `[project.optional-dependencies]` block references connector packages
  by version constraint, and pip needs those targets to already be on
  PyPI to resolve `pip install "genblaze[all]"`.
* **`publish-npm`** — independent of the PyPI graph. Publishes
  `@genblaze/spec` with sigstore provenance.
* **`install-verify`** — installs `genblaze[all]==$version` from public
  PyPI in a fresh venv and runs `tools/release_import_smoke.py`, which
  imports the umbrella, core, s3, and every connector in `genblaze[all]`.
  The `[all]` form exercises every connector's pin and import surface
  against the live registry, which is what catches drift like the 0.3.2
  langsmith/cli wheels (a bare `genblaze==$version` resolve only pulls
  the umbrella defaults and misses connector-specific pin breakage).
  Skipped on dry-runs (TestPyPI indexing lag makes this flaky).

### 2. Dry run (`workflow_dispatch`)

Use this to exercise the full pipeline without touching production
registries. From the GitHub Actions tab → Release workflow → "Run
workflow":

* **`dry_run`** — defaults to `true`. PyPI uploads target TestPyPI when explicitly enabled;
  npm publish runs with `--dry-run`. No production registry changes.
* **`validation_only`** — defaults to `true`. With `dry_run=true`, build and
  validate every wheel and run npm's publish dry run, but skip **all** Python
  uploads. The TestPyPI target stays selected. Set `validation_only=false`
  only to intentionally upload to TestPyPI; its trusted publishers are separate
  from production and must be configured. With `dry_run=false`, this option
  has no effect: all production signature/milestone gates apply.
* **`ref`** — optional. Defaults to the branch you dispatched from.

Dry runs are the recommended way to:

* Test the workflow after editing `release.yml`.
* Verify Trusted Publishing is configured correctly on a brand-new
  package before its first real publish.
* Sanity-check a release candidate without using up a version number on
  prod PyPI (you can't re-publish the same version; a botched 0.4.0
  burns the slot forever).

## One-time setup

Before this workflow can run successfully, three external setups must
exist:

### a. PyPI Trusted Publishing (per package)

For each of the ~15 Python packages, on its PyPI project page, add a
**Trusted Publisher** entry with:

* Repository owner: `backblaze-labs`
* Repository name: `genblaze`
* Workflow filename: `release.yml`
* Environment name: `pypi`

No `PYPI_API_TOKEN` is needed once Trusted Publishing is configured —
the workflow uses OIDC and the action obtains a short-lived token at
publish time.

### b. TestPyPI Trusted Publishing (per package, for dry-runs)

Same setup as above, but on **test.pypi.org** instead of pypi.org, with
environment name `testpypi`. This is what the dry-run path uses.

If you skip this, dry-runs will fail at the publish step (after building
and gating, which still gives you ~90% of the value).

### c. npm publish auth

`publish-npm` uses **npm Trusted Publishing** (OIDC); there is no npm token.
On npmjs.com, open `@genblaze/spec` → Settings → Trusted Publisher → GitHub
Actions, and set organization `backblaze-labs`, repository `genblaze`,
workflow `release.yml`, environment `npm`. It requires npm >= 11.5.1, which
is why the workflow pins `NODE_VERSION` to 24. Dry runs (`npm publish
--dry-run`) don't authenticate, so `npm-dryrun` needs no setup.

### d. GitHub Environments

Create four environments under repo Settings → Environments:

* **`pypi`** — production PyPI. No required reviewers.
* **`testpypi`** — TestPyPI for dry-runs. No required reviewers.
* **`npm`** — production npm. No required reviewers.
* **`npm-dryrun`** — npm dry-run. No required reviewers.

Required reviewers can be added later if you want a human gate on
production releases (recommended once the project graduates from alpha).

## Operational notes

* **Re-running a partial release is safe.** Both PyPI (`skip-existing:
  true` on the publish action) and the npm job (pre-check via `npm view`)
  treat "this version is already published" as a no-op success. So if
  half the connectors land and then a network blip kills the run,
  re-trigger the same Release event — published packages are skipped,
  unpublished ones go out.
* **Bumping a version because you _need_ a new artifact.** PyPI does
  not allow republishing the same version number even with `--force`;
  if a wheel was built with bad metadata and you need to fix it, you
  must bump the patch version (`0.3.0` → `0.3.1`) and re-tag.
* **Connector failures are recoverable.** Because the connector matrix
  uses `fail-fast: false`, a single failed connector leaves the others
  published. To recover, fix the issue, bump that connector's patch
  version, and trigger a new release — already-published packages are
  silently skipped.
* **The umbrella is the long pole.** If `publish-meta` fails, users
  cannot `pip install genblaze` — but they can still install individual
  packages. Treat a meta failure as a P0.
* **install-verify is best-effort.** PyPI's CDN sometimes lags. The
  umbrella pre-check retries for 2 minutes before failing, and the
  `genblaze[all]` install itself retries up to 5 times (30s apart) since
  any of its ~14 connector packages can propagate independently of the
  umbrella (#189). A red `install-verify` after a green publish graph
  usually means a package is fine and the index is just behind — verify
  manually with `pip install "genblaze[all]==X.Y.Z"` in a fresh venv
  before assuming a regression.

## Post-publish verification

`install-verify` runs inside the workflow, but it lives in the same
GitHub Actions runner that just published — same network, same caches.
After a release lands, do an independent check from your local machine
against public PyPI:

```bash
make post-release VERSION=0.4.0
```

`VERSION` is the **umbrella** version from `libs/meta/pyproject.toml`,
not the wave name (e.g. wave 0.3.0 shipped umbrella 0.4.0). The target
creates a throwaway venv in `/tmp`, installs `genblaze[all]==$VERSION`
from public PyPI, imports the umbrella, core, s3, and every connector in
`genblaze[all]`, then prints the installed versions of the umbrella/core/s3
packages. On failure the venv is left in place so you can re-run the failing
command interactively.

This is the same check that caught the 0.3.0 `genblaze-s3` dependency-
pin drift after `install-verify` lagged — it's a backstop, not
redundant.

## Releasing manually (fallback only)

If the workflow is unavailable, the release owner must approve an emergency
manual publication. The same readiness and trust checks still apply first:

```bash
make pre-release
make changelog-release-check
python tools/release_gate.py --tag vX.Y.Z
make release-smoke                    # build + install + import all
cd libs/core && python -m build && twine upload dist/*
# repeat for cli, every connector, then libs/meta last
cd libs/spec && npm publish --access public --provenance
```

Do not continue after a failed check. This fallback lacks the workflow's
OIDC identity and automated install-verify; record the reason, require the
release owner's approval, and run post-release verification explicitly.


## Non-publishing workflow validation

Pending fragments and pin mismatches still fail the normal gates on dry runs.
For a mid-wave tooling PR, prepare a separate, auditable candidate snapshot:
apply its package-version plan and batch its notes without tagging or publishing.
Dispatch the branch's workflow against that candidate's immutable SHA:

```bash
gh workflow run release.yml --ref "$WORKFLOW_BRANCH" \
  -f ref="$CANDIDATE_SHA" -f dry_run=true -f validation_only=true
```

This validates the TestPyPI-targeted build graph without registry uploads. It
exercises the four gates, metadata checks, type generation and npm dry run;
it does not claim to validate OIDC upload authentication or a production tag.
Do not remove fragments, relax assertions, or bypass a gate to obtain a green run.

Related proposal [#352](https://github.com/backblaze-labs/genblaze/pull/352)
removes TestPyPI entirely. Reconcile it before merging either overlapping
workflow change; this implementation retains the explicit TestPyPI route.
