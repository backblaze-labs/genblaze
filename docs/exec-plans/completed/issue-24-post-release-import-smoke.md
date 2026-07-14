# Issue 24: Shared Post-Release Import Smoke

## Issue

- GitHub issue: https://github.com/backblaze-labs/genblaze/issues/24
- Problem: `install-verify` and `make post-release` installed
  `genblaze[all]` from public PyPI but only imported `genblaze_core` and
  `genblaze_s3`.

## Changes

1. Added `tools/release_import_smoke.py` as the shared import smoke helper for
   release verification.
2. Updated `tools/release_smoke.sh`, the production `install-verify` workflow
   job, and `make post-release` to call the shared helper.
3. Added a tools test that compares the helper's package mapping against
   `libs/meta/pyproject.toml`'s `genblaze[all]` extra so future connectors
   cannot silently miss the post-release smoke.
4. Updated `RELEASING.md` to describe the all-module post-release import check.

## Verification

- `python -m pytest tools/tests/test_release_import_smoke.py`
- `bash -n tools/release_smoke.sh`
- `python -m compileall tools/release_import_smoke.py`
- `ruff check tools/release_import_smoke.py tools/tests/test_release_import_smoke.py`
- `ruff format --check tools/release_import_smoke.py tools/tests/test_release_import_smoke.py`
- `make release-smoke`
- `make post-release VERSION=0.4.1`
- `make install-dev`
- `make test`
- `make lint`
