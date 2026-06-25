<!-- created: 2026-06-25 -->
# Release 0.4.2 — Fast-follow patch

Three pre-existing bugs (not regressions from 0.4.1) confirmed against source.
Delivered as one PR off `origin/main`, TDD, minimal diff.

## Bugs in scope

| ID | Package | Root cause | Fix |
|----|---------|------------|-----|
| A | genblaze-core | `testing.py` top-level `import pytest` pollutes the runtime install; `MockProvider` / `MockVideoProvider` / `MockAudioProvider` are advertised as public API but require `pytest` at import | Split mocks into `genblaze_core/mocks.py` (no pytest); `testing.py` re-exports for backward compat |
| B | genblaze-cli | `extract -o/--output` documented in `cli/README.md:29` but the Click command has no such option | Add `-o/--output PATH` option; write JSON to path when given, stdout when omitted |
| C | genblaze-cli | `--version` output reads `genblaze, version 0.3.2` — looks like the umbrella | Pass `prog_name="genblaze-cli"` to `version_option` |
| D | genblaze-core | No smoke test exercises every `__all__` name at import; Fix A would have been caught | Add `test_all_public_names_importable.py` |

Fix E (google/nvidia `validate_model` stderr noise): investigated — both connectors already
route probe warnings through `logging.debug`. No stderr prints exist. **Deferred: not
applicable to current source.**

## Version plan

- `genblaze-core`: 0.3.4 → **0.3.5** (Fix A + D)
- `genblaze-cli`: 0.3.2 → **0.3.3** (Fix B + C)
- `genblaze` umbrella: stays at **0.4.1** — its core floor is `>=0.3.4,<0.4`; 0.3.5
  satisfies that constraint, so users get the fix via normal resolution. The umbrella's
  core floor must advance to `>=0.3.5,<0.4` to satisfy
  `test_umbrella_core_dependency_floor_matches_local_core`. The umbrella version itself
  does NOT need a bump.
- cli's `genblaze-core` floor must advance to `>=0.3.5,<0.4` to maintain the pin-parity
  invariant asserted by `test_cli_core_dependency_floor_matches_local_core`.

## File map

### Fix A — `genblaze_core/mocks.py` (new)
- Copy `MockProvider`, `MockVideoProvider`, `MockAudioProvider` from `testing.py`.
- No `import pytest` anywhere in this file.

### Fix A — `genblaze_core/testing.py` (edit)
- Remove the three class bodies; import and re-export them from `mocks.py`.
- Keep `ProviderComplianceTests` and the `import pytest` here (legitimate).

### Fix A — `genblaze_core/__init__.py` (edit)
- Rewire `_LAZY_IMPORTS` entries for `MockProvider`, `MockVideoProvider`,
  `MockAudioProvider` to point at `genblaze_core.mocks`.

### Fix A — `libs/core/pyproject.toml` (edit)
- Bump version 0.3.4 → 0.3.5.

### Fix D — `libs/core/tests/unit/test_all_public_names_importable.py` (new)
- Iterates `genblaze_core.__all__`, resolves each via `getattr`, asserts no AttributeError.
- Subprocess test: runs `python -c "import genblaze_core; genblaze_core.MockVideoProvider"`
  in a fresh interpreter (PYTHONPATH pointing only at the local core wheel) to prove the
  no-pytest path works when pytest is absent.

### Fix B — `cli/genblaze_cli/commands/extract.py` (edit)
- Add `@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)`.
- If `output` is set, write the JSON string to that path; otherwise `click.echo` to stdout.

### Fix C — `cli/genblaze_cli/main.py` (edit)
- Change `@click.version_option(package_name="genblaze-cli")` to
  `@click.version_option(package_name="genblaze-cli", prog_name="genblaze-cli")`.

### Fix B+C — `cli/pyproject.toml` (edit)
- Bump version 0.3.2 → 0.3.3.
- Advance `genblaze-core` floor to `>=0.3.5,<0.4`.

### Umbrella floor — `libs/meta/pyproject.toml` (edit)
- Advance `genblaze-core>=0.3.4,<0.4` → `>=0.3.5,<0.4`.

### Tests — `cli/tests/test_cli.py` (edit)
- Add `test_extract_output_flag_writes_file` — invoke extract with `-o`, assert file
  exists and contains valid JSON with `canonical_hash`.
- Add `test_version_label_shows_cli_package` — invoke `cli --version`, assert
  `genblaze-cli` in output.

### CHANGELOG.md (edit)
- Add entries under `[Unreleased]` for both packages.

## Sequencing

1. Branch from `origin/main` (done).
2. Write execution plan (done).
3. Implement Fix A: create `mocks.py`, edit `testing.py`, rewire `__init__.py`.
4. Add Fix D test (`test_all_public_names_importable.py`).
5. Implement Fix B + C: edit `extract.py` and `main.py`.
6. Add CLI tests to `test_cli.py`.
7. Apply version bumps: core 0.3.5, cli 0.3.3, floors in cli + umbrella.
8. Update CHANGELOG.md.
9. Run `make test` + `make lint`.
10. Run `make pre-release`.
11. Commit, push, open PR.

## Risks

- `test_cli_core_dependency_floor_matches_local_core` will fail until the cli floor
  is advanced to 0.3.5. Apply all version bumps together before running the full suite.
- `test_umbrella_core_dependency_floor_matches_local_core` will fail until the umbrella
  floor is advanced. Same remedy.
- Subprocess no-pytest test must use a fresh interpreter; not `monkeypatch sys.modules`
  (which leaves pytest importable in the same process). Use `subprocess.run` instead.
