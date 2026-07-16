---
name: prepare-release
description: Prepare a wave-level genblaze release — scope version bumps and dependency-floor drift with tools/prepare_release.py, apply them, gate with make pre-release, and open a release PR. Cannot tag or publish. Use before cutting a new CHANGELOG wave (as opposed to /release-check, which gates one already-decided package version).
allowed-tools: Bash(git add:*) Bash(git commit:*) Bash(git checkout:*) Bash(git switch:*) Bash(git push:*) Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(python3:*) Bash(make pre-release:*) Bash(make ts-types:*) Bash(gh pr:*) Read Grep Edit
---

# Prepare a release wave — $ARGUMENTS

**Safety boundary**: the `allowed-tools` list above deliberately has no `git tag`,
`gh release`, `twine upload`, or `make post-release`. This skill cannot tag or publish
anything — without a `git tag` step there is no tag to push, and without `gh release`
or `twine` there is nothing to trigger `.github/workflows/release.yml`. It goes as far
as a merge-ready release PR and stops. Tagging, releasing, and post-release
verification are human commands, emitted (not run) at the end.

This is a thin driver over [`tools/prepare_release.py`](../../../tools/prepare_release.py),
which does the actual bookkeeping (dynamic package discovery, version bumps, dependency
floor sync, reserved-core-version guard — see its module docstring). For what a wave
release actually IS (versioning policy, the publish pipeline graph, tag-naming
convention) see [RELEASING.md](../../../RELEASING.md) — this skill does not restate
it. For gating one already-decided package version one at a time, use `/release-check`
instead; this skill operates at the wave level across every package at once.

## Step 1 — Scope

```bash
python3 tools/prepare_release.py --check
```

Read the report: which packages changed since the last tag, what version bumps and
dependency-floor updates it computes, and any warnings (e.g. a connector with no
entry in `libs/meta/pyproject.toml`). Exit 0 means nothing to prepare — stop here.
Exit 1 means proceed to Step 2. Exit 2 is a hard error (bad `--set`/`--bump`, a git
failure, or the reserved-core-version guard tripping) — resolve it before continuing.

Before doing anything else, confirm:
- You're on `main` (`git status`), up to date with `origin/main`, and the working
  tree is clean.
- `git log` shows the commit you're about to release from is the one you intend.

## Step 2 — Apply, then cut the CHANGELOG

```bash
python3 tools/prepare_release.py --apply
```

This writes every version bump and dependency-floor update computed in Step 1. Pass
`--set <pkg>=<version>` or `--bump <pkg>=minor|major` to override the default patch
bump for any package (e.g. `--set core=0.4.0` for a deliberate minor/major, or to
route around the reserved-version guard intentionally).

Then, by hand (this is not scriptable — it's an editorial decision about wave naming
and prose):

1. Cut `CHANGELOG.md`'s `[Unreleased]` section to `## [X.Y.Z] - <today>`, pasting in
   the `### Released package versions` list the script just emitted. Fold in any
   entries that were mis-sectioned under the wrong package heading.
2. Leave `[Unreleased]` empty — `changelog-gate` in `release.yml` fails the release
   otherwise.
3. **The tag name is `v` + that heading, exactly** (e.g. heading `## [0.5.0]` → tag
   `v0.5.0`) — `validate-version` enforces this and a mismatch is the single most
   common reason a release run fails. Do not confuse the wave name with any one
   package's version (see RELEASING.md's versioning policy).

If `libs/spec` changed this wave, `prepare_release.py` will have flagged it — run:

```bash
make ts-types
```

and commit the regenerated `libs/spec/ts/genblaze.d.ts` in the same PR.

## Step 3 — Gate

```bash
make pre-release
```

This runs the same lint/typecheck/ts-types/pypi-metadata/pin-parity/test/release-smoke
gates the release workflow runs (see RELEASING.md's Pre-release checklist) — a clean
run here is a strong signal `validate-version`, `changelog-gate`, and `release-smoke`
will all be green once tagged. Delegate any single-package specifics (entry-point
sanity, per-connector doc freshness) to `/release-check`.

**macOS prerequisites** — verify these are already satisfied before running the
gate; this skill has no permission to install anything, and one-time machine setup
(cert installation) should never be automated by a skill:
- An activated venv so `python`/`pip` resolve (`make pre-release` shells out to
  `python -m build`, `mypy`, etc.)
- `mypy>=1.8` and `pip install build twine` already done in that venv
- If the PyPI-metadata/pin-parity checks fail with an SSL error, run
  `/Applications/Python 3.x/Install Certificates.command` once (a one-time,
  machine-level change — do this yourself, don't script it), or set
  `export SSL_CERT_FILE=$(python3 -m certifi)` for the session.

If `make pre-release` can't run locally for environment reasons (missing Node for
`ts-types`, no local PyPI network access, etc.), fall back to the `workflow_dispatch`
dry-run instead of forcing it: push the branch, then from the Actions tab → Release
workflow → "Run workflow" with `dry_run: true`. This exercises the identical
pipeline against TestPyPI without a local toolchain.

## Step 4 — Open the release PR

```bash
git switch -c release/<wave>
git add -A
git commit -m "chore(release): bump versions and cut CHANGELOG <wave> wave"
git push -u origin release/<wave>
gh pr create --title "chore(release): <wave> wave" --body "..."
```

Never commit to `main` directly. This is a normal PR — it needs review and a green
CI run like anything else before it merges.

**Conditional follow-ups** (only if the wave warrants them, not mandatory steps):
- Breaking changes this wave? Write `MIGRATING-<wave>.md`.
- Large surface touched? Consider running `/verify-docs` and `/security-review`
  before opening the PR.

## After the PR merges

Once CI is green on `main`, here are the human commands to run — this skill does
not run them for you:

```bash
git tag -a v<wave> -m "Release <wave>"
git push origin v<wave>
gh release create v<wave> --title "<wave>" --notes-from-tag
# after the Release workflow finishes publishing:
make post-release VERSION=<umbrella-version>
```

`<umbrella-version>` is `libs/meta/pyproject.toml`'s version, which may differ from
the wave name (see RELEASING.md — e.g. wave `0.3.0` shipped umbrella `0.4.0`).
