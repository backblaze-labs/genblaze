# Functional Integrity Checklist

Run through every item. Mark `[x]` when verified, `[!]` when a problem is found.

## Build & Import
- [ ] `pip install -e libs/core` succeeds
- [ ] `python3 -c "import genblaze_core"` works
- [ ] `python3 -c "from genblaze_core import Pipeline, Manifest, Run, Step, Asset"` works
- [ ] All connector packages install without errors
- [ ] CLI package installs and `genblaze --help` responds

## Test Suite
- [ ] `make test` — all tests pass (record count: ___ / ___)
- [ ] `make coverage` — meets 70% minimum (record: ___%)
- [ ] No tests are skipped without a documented reason
- [ ] No flaky tests (run suite twice if suspect)
- [ ] Test fixtures don't rely on network access

## Examples Validation
- [ ] All 20 examples in `examples/` parse without syntax errors:
  ```bash
  for f in examples/*.py; do python3 -m py_compile "$f" && echo "OK: $f" || echo "FAIL: $f"; done
  ```
- [ ] Quickstart examples match README code snippets
- [ ] Storage examples reference correct import paths
- [ ] Chain/fan-in examples demonstrate multi-step patterns

## CLI Commands
- [ ] `genblaze extract --help` works
- [ ] `genblaze verify --help` works
- [ ] `genblaze replay --help` works
- [ ] `genblaze index --help` works

## Cross-Package Dependencies
- [ ] Connectors can import from `genblaze_core` without circular deps
- [ ] CLI can import from `genblaze_core` without issues
- [ ] Optional deps (pyarrow, mutagen) fail gracefully when missing

## Public API Surface
- [ ] `genblaze_core/__init__.py` exports match documented API
- [ ] No private symbols (prefixed `_`) are exported
- [ ] All exported classes have `__all__` defined
- [ ] Version string in `_version.py` matches `pyproject.toml`

## Data Model Integrity
- [ ] Pydantic models validate with sample data
- [ ] Manifest round-trip: create → serialize → deserialize → verify hash
- [ ] All enum values are valid and documented
- [ ] UUID generation produces valid v4 UUIDs
