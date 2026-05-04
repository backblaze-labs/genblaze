"""Reject regex patterns prone to catastrophic backtracking.

A ``ModelFamily``'s pattern is a public surface — connector authors write it
once at module load, and every slug a user submits is matched against it
during ``Pipeline.run()`` preflight. A poorly-written pattern with nested
unbounded quantifiers (``(a+)+``, ``(.+)+``, ``(a|a)*``) on adversarial
input can take seconds to match a single string, turning preflight into a
DoS vector.

This module guards against that at ``ModelFamily`` construction time —
patterns that fail the safety check raise ``ValueError`` during connector
import, before any user code runs. Two strategies, evaluated in order:

* If ``google-re2`` is installed, every pattern is recompiled through it
  to confirm linear-time matching. ``re2`` rejects unsupported constructs
  outright, so the check is authoritative.
* Otherwise, fall back to a static heuristic that flags the most common
  catastrophic-backtracking shapes — nested unbounded quantifiers and
  alternations of identical branches.

The heuristic is conservative on purpose. It rejects clearly-bad patterns;
it does not pretend to detect every pathological case. Connector authors
who hit a false positive can rewrite the pattern (typically by anchoring
or making quantifiers possessive). Authors who write subtly-bad patterns
that slip past the heuristic are caught by the perf gates in
``tests/perf/test_registry_perf.py`` (P99 < 100 µs on adversarial inputs).
"""

from __future__ import annotations

import re
from typing import Final

try:
    import google.re2 as _re2  # type: ignore[import-not-found]

    _HAS_RE2 = True
except ImportError:
    _HAS_RE2 = False


# Heuristic patterns indicating a likely catastrophic-backtracking risk:
#   * a group containing an unbounded quantifier (``+`` or ``*``), itself
#     followed by another unbounded quantifier — the canonical ``(a+)+`` shape.
#   * an alternation of identical branches inside an unbounded quantifier —
#     the ``(a|a)*`` shape.
# Both are conservative; false positives are preferred over false negatives.
_NESTED_QUANTIFIER: Final[re.Pattern[str]] = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*]")
_DUPLICATE_ALTERNATION: Final[re.Pattern[str]] = re.compile(r"\(([^)|]+)\|\1\)\s*[+*]")


def assert_safe(pattern: re.Pattern[str]) -> None:
    """Raise ``ValueError`` if ``pattern`` looks unsafe to match.

    Call from ``ModelFamily.__post_init__`` so connector imports fail fast
    when a pattern would put preflight at risk under adversarial input.
    """
    src = pattern.pattern

    if _HAS_RE2:
        try:
            _re2.compile(src)
        except Exception as exc:
            raise ValueError(
                f"Pattern {src!r} rejected by google-re2 "
                f"(linear-time guarantee unavailable): {exc}"
            ) from exc
        return

    if _NESTED_QUANTIFIER.search(src) or _DUPLICATE_ALTERNATION.search(src):
        raise ValueError(
            f"Pattern {src!r} has nested unbounded quantifiers or duplicate "
            f"alternation branches and is rejected to prevent catastrophic "
            f"backtracking on adversarial input. Rewrite the pattern (anchor "
            f"it, use non-capturing groups, or make quantifiers possessive) "
            f"or install google-re2 for an authoritative linear-time check."
        )


def has_re2() -> bool:
    """Return ``True`` if ``google-re2`` is installed and active.

    Exposed for tests and observability — the registry doesn't change
    behavior based on this; ``assert_safe`` already chose the strategy.
    """
    return _HAS_RE2
