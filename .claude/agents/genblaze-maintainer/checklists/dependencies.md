# Dependency Health Checklist

Run through every item. Mark `[x]` when verified, `[!]` when a problem is found.

## Version Constraints
- [ ] `genblaze-core` pyproject.toml: Python >=3.11 enforced
- [ ] All connector pyproject.toml files: Python >=3.11 enforced
- [ ] CLI pyproject.toml: Python >=3.11 enforced
- [ ] Pydantic pinned to >=2.0 (no v1 compatibility)
- [ ] Pillow pinned to >=10.0
- [ ] All version bounds are minimum (>=), not exact (==)

## Optional Dependencies
- [ ] `pyarrow` gated behind `[parquet]` extra
- [ ] `mutagen` gated behind `[audio]` extra
- [ ] Code gracefully handles missing optional deps:
  ```python
  try:
      import pyarrow
  except ImportError:
      raise ImportError("Install genblaze-core[parquet]")
  ```
- [ ] Optional features are documented with install instructions

## Provider Packages
- [ ] Each connector declares `genblaze-core` as dependency
- [ ] Provider-specific deps are isolated to their package
- [ ] No provider package depends on another provider package
- [ ] API client libraries use compatible versions

## Dev Dependencies
- [ ] pytest, pytest-asyncio in dev/test groups only
- [ ] ruff, mypy in dev/lint groups only
- [ ] Dev deps don't appear in runtime `dependencies`
- [ ] `make install-dev` installs all dev tooling

## Supply Chain
- [ ] All packages sourced from PyPI (no private indexes)
- [ ] No vendored copies of external libraries
- [ ] No `git+https://` dependencies
- [ ] Run `pip-audit` if available:
  ```bash
  pip-audit --desc 2>/dev/null || echo "pip-audit not installed"
  ```

## Compatibility Matrix
- [ ] Python 3.11 tested
- [ ] Python 3.12 tested
- [ ] Python 3.13 tested (if CI supports it)
- [ ] Pydantic v2 latest minor works
- [ ] No deprecation warnings from dependencies:
  ```bash
  python3 -W all -c "import genblaze_core" 2>&1 | grep -i deprecat
  ```

## Lock Files & Reproducibility
- [ ] No `requirements.txt` overriding pyproject.toml
- [ ] `pyproject.toml` is the single source of truth for deps
- [ ] CI uses same dependency resolution as local dev
