# Code Quality Checklist

Run through every item. Mark `[x]` when verified, `[!]` when a problem is found.

## Linting & Formatting
- [ ] `make lint` passes with zero violations
- [ ] `make fmt` produces no changes (code already formatted)
- [ ] `make typecheck` — record number of errors: ___
- [ ] Ruff rules (E, F, I, UP, B, S) are all enabled and passing

## Type Safety
- [ ] All public functions have type annotations
- [ ] `py.typed` marker is present (PEP 561)
- [ ] Generic types used correctly (Runnable[In, Out])
- [ ] No `Any` types in public API signatures
- [ ] Pydantic v2 model_validator/field_validator patterns are correct

## Error Handling
- [ ] Custom exceptions used (no raw `Exception` raises)
- [ ] All exceptions in `exceptions.py` are documented
- [ ] No bare `except:` clauses — specific exception types caught
- [ ] Async exception handling preserves context
- [ ] Resources cleaned up in `finally` blocks where needed

## Code Organization
- [ ] Imports follow order: stdlib → third-party → local
- [ ] No circular imports between modules
- [ ] No wildcard imports (`from x import *`)
- [ ] Module-level code is minimal (no side effects on import)
- [ ] Constants are UPPER_CASE and collected at module top

## Documentation
- [ ] All public classes have docstrings
- [ ] All public functions have docstrings
- [ ] Docstrings include parameter descriptions
- [ ] Complex algorithms have inline comments
- [ ] No outdated comments that contradict code

## Test Quality
- [ ] Tests use descriptive names (`test_pipeline_raises_on_timeout`)
- [ ] No `assert True` or `assert 1 == 1` (meaningful assertions)
- [ ] Tests cover both happy path and error cases
- [ ] Async tests use `pytest-asyncio` correctly
- [ ] Test fixtures are shared via `conftest.py`
- [ ] No test data depends on network or external services
- [ ] Mocks are used appropriately (not over-mocking)

## Dead Code & Debt
- [ ] No unused imports
- [ ] No unreachable code branches
- [ ] TODO/FIXME comments reviewed:
  ```bash
  grep -rn "TODO\|FIXME\|HACK\|XXX" --include="*.py" libs/ cli/
  ```
- [ ] Dead code removed or tracked in tech-debt-tracker
- [ ] No commented-out code blocks

## Patterns & Consistency
- [ ] Builder pattern is consistent (StepBuilder, RunBuilder)
- [ ] Provider lifecycle follows submit/poll/fetch_output pattern
- [ ] Canonical JSON follows documented normalization rules
- [ ] Logging uses structured logger, not print()
- [ ] All async functions are properly awaited (no fire-and-forget)
