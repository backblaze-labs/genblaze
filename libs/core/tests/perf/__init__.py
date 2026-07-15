"""Performance regression gates.

Distinct from the correctness suites in ``tests/unit/``: these tests assert
timing budgets rather than behavior. Kept in their own package so they can be
skipped or run in isolation (``pytest tests/perf/``) if a CI runner turns out
to be too noisy for microsecond-level assertions.
"""
