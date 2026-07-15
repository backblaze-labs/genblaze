<!-- last_verified: 2026-07-15 -->
# ReDoS guard regression: heuristic must always run regardless of re2 (#148)

## Problem

`pattern_safety.assert_safe()` (added by #146/#80) returns early after a
successful `_re2.compile(src)` when `google-re2` is installed, skipping
`_heuristic_unsafe()`. `re2.compile()` only confirms a pattern is
*syntactically compatible* with re2 — it accepts nested-quantifier shapes
like `(a+)+$` because re2's own engine matches them in linear time. But
`ModelFamily.matches()` (`family.py:217`) always matches with a stdlib
`re.Pattern`, which still backtracks catastrophically on the same input.
Net effect: in any environment with `re2` installed (CI, `[dev]`, `[re2]`),
the guard is strictly weaker than the pre-#146 heuristic-only behavior for
exactly the shapes it was built to catch.

## Fix

Drop the early `return` in `assert_safe()` — `re2` becomes an *additional*
gate (still rejects patterns re2 itself can't support, e.g. backreferences)
rather than a replacement for the heuristic. `_heuristic_unsafe()` now runs
unconditionally.

Files touched:
- `libs/core/genblaze_core/providers/pattern_safety.py` — remove the early
  `return`; update the module/function docstrings that claimed re2 was a
  standalone "authoritative" replacement.
- `libs/core/tests/unit/test_pattern_safety.py` — remove the now-incorrect
  `skipif(has_re2())` markers on the existing heuristic-shape rejection
  tests (they should hold regardless of re2), and add a parametrized
  regression test for the issue's exact repro shapes (`(a+)+`, `([a-z]+)+`,
  `(v\d+)+.*`) that monkeypatches `_HAS_RE2` (and a fake `_re2.compile`) to
  exercise both branches deterministically, independent of whether re2
  happens to be installed in the running environment.

No change needed in `family.py` — its matching code and docstring already
describe the guard's intent correctly; the bug was entirely in
`pattern_safety.py`'s branch logic.

## Risk

Low. Environments with `re2` installed now pay one extra heuristic scan per
`ModelFamily` construction (import-time only, not a hot path) — negligible
cost, and the existing perf gate (`tests/perf/test_registry_perf.py`)
already budgets for the heuristic running.
