<!-- created: 2026-07-01 -->
# PyPI README relative link gate

**Status:** Complete (2026-07-01) · **Issue:** #14 · **Shape:** D + test/tooling guard

## Goal

Fix the GMICloud and NVIDIA package READMEs so links rendered on PyPI resolve, and
extend the PyPI metadata gate so relative Markdown links in shipped Markdown
READMEs fail before release.

## Changes

| File | Change |
|------|--------|
| `libs/connectors/gmicloud/README.md` | Replaced relative docs links with absolute GitHub blob URLs. |
| `libs/connectors/nvidia/README.md` | Replaced the relative pricing docs link with an absolute GitHub blob URL. |
| `tools/check_pypi_metadata.py` | Reads package `readme` files from published pyprojects and rejects relative Markdown link targets. |
| `tools/tests/test_check_pypi_metadata.py` | Covers rejection of relative README links and allowance of absolute URLs, anchors, and mailto links. |

## Verification

- `make pypi-metadata-check`
- `pytest tools/tests -v`
- `make test`
- `make lint`
- `ruff check tools/check_pypi_metadata.py tools/tests/test_check_pypi_metadata.py`
- `ruff format --check tools/check_pypi_metadata.py tools/tests/test_check_pypi_metadata.py`
