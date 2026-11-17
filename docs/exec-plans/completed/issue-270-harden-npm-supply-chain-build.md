<!-- completed: 2026-09-03 -->
# Issue 270: Harden the Node build toolchain against npm supply-chain attacks (ignore-scripts, lockfile, split publish job)

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/270
Labels: `ci-release`, `P2`, `security`

Preventive hardening prompted by the Aug 2026 keyv/cacheable npm supply-chain
campaign, where malware stole GitHub Actions OIDC and npm tokens via a
`preinstall` hook and self-propagated through npm Trusted Publishing.

**genblaze was NOT exposed.** The published `@genblaze/spec` has zero npm
dependencies, and the `json-schema-to-typescript@15.0.4` tree (15 packages)
contains none of the compromised keyv/cacheable/flat-cache/file-entry-cache/
cacheable-request/cache-manager packages. This closes the *structural* gap so
a future transitive compromise cannot reach a privileged context — it was not
a response to an active exploit.

The gap: `libs/spec/scripts/generate-types.sh` ran
`npx --yes json-schema-to-typescript@15.0.4` (three times) with no committed
lockfile, so the generator's transitive dependencies were resolved **fresh at
run time** from `^`-range specifiers. Verified live before the fix:

    $ npm view json-schema-to-typescript@15.0.4 dependencies
    { lodash: '^4.17.21', 'is-glob': '^4.0.3', 'js-yaml': '^4.1.0',
      minimist: '^1.2.8', prettier: '^3.2.5', tinyglobby: '^0.2.9',
      '@types/lodash': '^4.17.7', '@types/json-schema': '^7.0.15',
      '@apidevtools/json-schema-ref-parser': '^11.5.5' }

Every `^` means "resolve the newest matching version now" — so a future
compromise of any of these (or their own transitive `^` deps) would have been
pulled automatically on the next run. That live, script-enabled `npx` ran in
two contexts:

- CI `ts-types` job (`.github/workflows/ci.yml`) — low privilege. Also ran a
  second unpinned `npx --yes -p typescript@5.4 tsc`.
- Release `publish-npm` job (`.github/workflows/release.yml`) — holds
  **`id-token: write`** for npm provenance and carries `secrets.NPM_TOKEN`;
  ran `make ts-types` → `generate-types.sh` in that same privileged context.

An unpinned, script-enabled `npx` inside `publish-npm` was exactly the
context where a future transitive compromise would do the most damage: two
stealable secrets (the long-lived `NPM_TOKEN` and the OIDC token) were in
reach of any install-time hook — the keyv/cacheable malware's exact playbook.

Stale-comment note: `generate-types.sh`'s header said "no package.json in the
repo", but `libs/spec/package.json` already existed (publishing metadata, 0
runtime deps). The comment predated it and was corrected.

## Fix

CI/release hardening. **No change to the published `@genblaze/spec` contents
and no change to the generated `libs/spec/ts/genblaze.d.ts`** — verified
byte-for-byte identical. Three changes, highest-security-value first.

1. **Split build from publish (`.github/workflows/release.yml`)** — the
   structural fix. Added a low-privilege `build-npm-types` job (default
   read-only permissions) that runs `make ts-types`, verifies no drift, and
   uploads `libs/spec/ts/genblaze.d.ts` as an artifact. `publish-npm` now
   `needs:` it, downloads the artifact, and no longer regenerates or verifies
   types itself. Result: **no npm install or execution happens in the job
   holding `id-token: write`** — it only downloads a pre-built,
   drift-verified file and calls `npm publish`.

2. **Disabled install scripts** on every type-gen npm invocation
   (`--ignore-scripts`) in `generate-types.sh` and the `ci.yml` `ts-types`
   job (both the generator install and the `tsc` type-check). Neutralizes the
   entire `preinstall`/`postinstall` hook class at zero cost — neither tool
   needs lifecycle scripts to run.

3. **Pinned the toolchain via a committed lockfile.** Added
   `json-schema-to-typescript@15.0.4` and `typescript@5.4.5` as
   `devDependencies` in `libs/spec/package.json`; generated and committed
   `libs/spec/package-lock.json` (16 packages, all exact versions, none from
   the compromised list). Rewrote `generate-types.sh` to
   `npm ci --ignore-scripts --prefix libs/spec` then call the
   locally-installed `json2ts` binary instead of `npx --yes`. Fixed the
   stale header comment. `devDependencies` are not published, so the
   tarball is unaffected.

4. **Updated CI to use the lockfile (`.github/workflows/ci.yml`).** The
   `ts-types` job now caches on `libs/spec/package-lock.json` and calls the
   pinned local `tsc` (`libs/spec/node_modules/.bin/tsc`) — no unpinned `npx`
   remains anywhere in the type-generation path.

5. **Docs.** Updated `libs/spec/README.md`'s regeneration section to describe
   the lockfile-based install and how to bump pinned versions; added a
   `.gitignore` entry for `node_modules/` (the repo had none — it was purely
   Python before); added a `**Security**` bullet to `CHANGELOG.md` under
   `[Unreleased]` → `### Internal`.

### Regression / guard

The drift guard (`git diff --exit-code libs/spec/ts/`) is preserved and now
runs in the low-privilege `build-npm-types` job instead of `publish-npm`.
`npm ci` adds a second, independent reproducibility guard: it hard-fails on
any lockfile/manifest mismatch instead of silently re-resolving versions.

No version bump — package versions bump per release wave, not per fix
(RELEASING.md). `@genblaze/spec` `version` is untouched.

## Risk

Low. All CI/release plumbing; no runtime/API surface change and no change to
shipped artifacts.

- **Release workflow topology changed** — `publish-npm` now depends on a new
  `build-npm-types` job via an artifact hand-off. Not yet exercised end-to-end
  against real GitHub Actions (no `workflow_dispatch` dry-run was triggered as
  part of this change); both workflow YAML files were validated for correct
  structure and job dependencies, and `zizmor` (the repo's GH Actions security
  auditor) reports no findings against either modified file.
- **Lockfile bootstrapping** — the lockfile was generated fresh against the
  pinned `devDependencies` and committed together; `npm ci` confirmed to
  install cleanly from it.
- **Dependabot** — `devDependencies` are now Dependabot-visible; existing
  `ci.yml`/`security.yml` Dependabot guards already skip CI on those PRs, so
  no new CI exposure.

Out of scope: the PyPI publish graph in `release.yml` (separate ecosystem);
`genblaze-*` PyPI packages (Python, unaffected by an npm-ecosystem attack);
the published `@genblaze/spec` contents (0 runtime deps, unchanged).

## Files Modified

| File | Change | Notes |
|---|---|---|
| `.github/workflows/release.yml` | modified | new low-priv `build-npm-types` job builds + drift-checks + uploads `genblaze.d.ts` artifact; `publish-npm` downloads it instead of regenerating — no npm install/exec in the `id-token: write` job |
| `.github/workflows/ci.yml` | modified | `ts-types` job caches on the lockfile; `tsc` type-check uses the pinned local binary instead of unpinned `npx` |
| `libs/spec/scripts/generate-types.sh` | modified | installs from the lockfile with `npm ci --ignore-scripts` instead of `npx --yes`; fixed stale "no package.json" comment |
| `libs/spec/package.json` | modified | added `devDependencies` (`json-schema-to-typescript@15.0.4`, `typescript@5.4.5`) and `generate`/`typecheck` scripts |
| `libs/spec/package-lock.json` | created | committed lockfile pinning the full 16-package transitive tree |
| `.gitignore` | modified | added `node_modules/` (repo had no Node ignore rules before) |
| `libs/spec/README.md` | modified | regeneration section describes the lockfile-based install and version-bump procedure |
| `CHANGELOG.md` | modified | `**Security**` bullet under `[Unreleased]` → `### Internal` |
| `docs/exec-plans/completed/issue-270-harden-npm-supply-chain-build.md` | created | this file |

## Acceptance criteria (from the issue)

- [x] `generate-types.sh` and the `ci.yml` / `release.yml` jobs that call it
      run npm with install scripts disabled.
- [x] The type-generator dependency set is version-pinned and reproducible
      from a committed lockfile.
- [x] No npm install or execution happens in any job that holds
      `id-token: write`.

Additional invariants preserved:

- [x] `make ts-types` still produces a byte-for-byte identical
      `libs/spec/ts/genblaze.d.ts` (drift guard passes).
- [x] The published `@genblaze/spec` tarball is unchanged (devDependencies
      excluded from the `files` allowlist).

## Verification

Confirmed the generated output is unaffected by the hardening — same file,
safer production. Before the fix, from `main`:

    $ npx --yes json-schema-to-typescript@15.0.4 ...   # (three unpinned calls)
    Generated /.../libs/spec/ts/genblaze.d.ts

After the fix, same command surface (`make ts-types`), now installing from
the lockfile with scripts disabled:

    $ libs/spec/scripts/generate-types.sh

    added 16 packages, and audited 17 packages in 554ms

    6 packages are looking for funding
      run `npm fund` for details

    found 0 vulnerabilities
    Generated /Users/ffumero/Desktop/dev/genblaze/libs/spec/ts/genblaze.d.ts

Drift check against the pre-change committed file — confirms byte-identical
output:

    $ git diff --exit-code libs/spec/ts/
    NO DRIFT — output unchanged from committed main

Lockfile install (`npm ci`, the stricter CI-faithful install) from a clean
`node_modules/`:

    $ rm -rf libs/spec/node_modules && cd libs/spec && npm ci --ignore-scripts
    added 16 packages, and audited 17 packages in 531ms
    found 0 vulnerabilities

Pinned local `tsc` type-check (replacing the old unpinned
`npx --yes -p typescript@5.4 tsc`):

    $ libs/spec/node_modules/.bin/tsc --noEmit --strict --skipLibCheck libs/spec/ts/genblaze.d.ts
    TSC OK  (no output — exit 0)

Resolved lockfile packages (16 total) — confirmed none match the
keyv/cacheable/flat-cache/file-entry-cache/cacheable-request/cache-manager
compromise list:

    @apidevtools/json-schema-ref-parser, @jsdevtools/ono, @types/json-schema,
    @types/lodash, argparse, fdir, is-extglob, is-glob, js-yaml,
    json-schema-to-typescript, lodash, minimist, picomatch, prettier,
    tinyglobby, typescript

GitHub Actions workflow validation — both modified YAML files parse
correctly with the expected job graph:

    release.yml jobs: [..., publish-meta, build-npm-types, publish-npm, install-verify]
    build-npm-types.needs: ['validate-version']
    publish-npm.needs: ['build-npm-types', 'validate-version', 'changelog-gate', 'pin-parity']
    ci.yml jobs: [lint, ts-types, typecheck, deptry, build, test]

`zizmor` (the repo's GitHub Actions security auditor, run locally at v1.30.0
against both modified workflow files):

    $ zizmor .github/workflows/release.yml .github/workflows/ci.yml
    No findings to report. Good job! (2 ignored, 22 suppressed)

(One `cache-poisoning` finding was raised and fixed during implementation: an
initial `cache: "npm"` on the new `build-npm-types` job's `setup-node` step
was flagged because that job's output feeds the `id-token: write` publish
job via artifact — removed, matching the no-cache pattern already used in
`publish-npm` itself.)

Lint (no Python files touched by this change; run for completeness):

    $ ruff check libs/ cli/ examples/
    All checks passed!

    $ ruff format --check libs/ cli/ examples/
    400 files already formatted

`make test` was not run: this change touches only `.github/workflows/`,
`libs/spec/` (types tooling, not Python code), `.gitignore`, `CHANGELOG.md`,
and docs — no Python source or test file changed.

**Not yet done:** an actual `workflow_dispatch` dry-run (`dry_run=true`)
against real GitHub Actions, to exercise the `build-npm-types` →
`publish-npm` artifact hand-off and confirm `npm publish --dry-run
--provenance` still succeeds end-to-end. Recommended before the next release
tag.
