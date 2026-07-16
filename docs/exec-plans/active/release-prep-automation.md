<!-- created: 2026-07-16 -->
# Wave Release-Prep Automation

Adds a deterministic engine for the bookkeeping half of cutting a genblaze release
wave (RELEASING.md), plus a thin skill driver over it. Motivated by a concrete
failure mode from this session: a hand-edited release-prep pass bumped
`genblaze-core` but silently missed updating `cli`'s `genblaze-core` dependency
floor to match — invisible until a much later CI run. That's exactly the drift class
`tools/check_pin_parity.py` already guards against for connectors (`genblaze-s3` in
0.3.0, `genblaze-langsmith` + `genblaze-cli` in 0.3.2); this closes the same gap one
step earlier, before a human even has to remember to run the guard.

## Goals & success criteria

- No hardcoded package list or version literal anywhere in the new script — package
  set comes from fixed structural roots (`libs/core`, `cli`, `libs/meta`,
  `libs/spec`) plus a glob over `libs/connectors/*`.
- `cli`'s and the `genblaze` umbrella's `genblaze-core`/`genblaze-s3`/connector-extra
  dependency floors are always resynced to the FINAL decided version of the package
  they point at, whether or not that package changed this wave — the existing
  `test_cli_core_dependency_floor_matches_local_core` /
  `test_umbrella_core_dependency_floor_matches_local_core` /
  `test_umbrella_s3_dependency_floor_matches_local_s3` /
  `test_umbrella_connector_extra_floors_match_local_versions` invariants in
  `cli/tests/test_cli.py` stay green regardless of which packages moved.
- Refuses to bump `genblaze-core` onto the version reserved for the
  `raise_on_failure` default flip (`_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION` in
  `libs/core/genblaze_core/pipeline/pipeline.py`), read from source rather than
  duplicated as a literal in the script.
- Idempotent: re-running `--apply` (or hand-bumping a package outside the tool) is a
  safe no-op for anything already past what was published at the last tag.
- The driving skill (`.claude/skills/prepare-release/SKILL.md`) cannot tag or
  publish — enforced via its `allowed-tools` frontmatter, not prose.

## Key decisions

- **Text-level pyproject/package.json surgery, not full TOML re-serialization.**
  Targeted regex line rewrites preserve comments and formatting; matches how the
  rest of `tools/` (e.g. `validate-version` in `release.yml`) already treats these
  files as text, not just data.
- **Floor rewriting preserves the existing upper-bound cap verbatim** (e.g. the
  `<0.4` in `genblaze-core>=0.3.6,<0.4`) — only the floor number is substituted, so
  the script never hardcodes that cap and stays correct once it eventually moves.
- **"Already bumped" is detected by comparing on-disk version to what was published
  at the last tag**, not by tracking script-internal state across runs — this is
  what makes re-running safe and also what let a live smoke-test (`--check` against
  `main`, last tag `v0.4.0`) correctly report the already-prepped 0.5.0 wave as
  clean except for one genuine, previously invisible gap (see Verification).
- **A pin that needs to change forces a version bump on the file that carries it**,
  even with no other code change to that package — this is the actual fix for the
  motivating bug, not a side effect.
- **New connectors with no existing floor line in `libs/meta/pyproject.toml` are not
  auto-wired** (extra + bundle membership is an editorial call) — surfaced as a
  warning instead.
- Design was red-teamed via a fork sub-agent before implementation (idempotency
  heuristic, reserved-version guard blind spot on `--check`, skill `allowed-tools`
  vs. its own prescribed macOS prereqs, over-generalized TOML editor) and again by
  a 3-reviewer panel over the finished diff before push.

## Verification

- `pytest tools/tests/ -v` — 86 passed (34 new for `prepare_release.py`, no
  regressions in `check_pin_parity`/`check_pypi_metadata`/`release_import_smoke`
  tests).
- `python3 tools/prepare_release.py --check` against the live repo (last tag
  `v0.4.0`) correctly recognized every package the 0.5.0 wave-prep commit
  (`afe1dd5`) already bumped as "no action needed," and surfaced exactly one real,
  previously invisible gap: `genblaze-nvidia`'s only change since `v0.4.0` is a
  README relative-link fix that was never version-bumped, so the broken-link
  README is still what's live on PyPI for that package.
- `ruff check` / `ruff format --check` clean on both new files.

## Follow-ups (not in this change)

- Whether to fold the `genblaze-nvidia` README gap the smoke test surfaced into a
  future patch wave is a release-scheduling decision, not something this PR
  resolves.
- Each connector's own `genblaze-core>=X,<0.4` floor (as opposed to `cli`'s and the
  umbrella's) is intentionally left wide/unsynced per existing convention (see the
  0.5.0 CHANGELOG: "All other connectors unchanged — their existing floor already
  admits 0.3.6") — not a gap, a deliberate scope boundary.
