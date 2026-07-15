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
  outright, so the check is authoritative. Install it via the ``re2``
  extra (``pip install "genblaze-core[re2]"``) or the ``dev`` extra, which
  includes it — CI installs ``libs/core[dev]``, so this path is active
  there by default.
* Otherwise, fall back to a static heuristic that flags the most common
  catastrophic-backtracking shapes: nested unbounded quantifiers (bare
  ``+``/``*`` or an open-ended ``{n,}`` brace quantifier, at any nesting
  depth), alternations of identical branches, and runs of the same atom
  quantified back-to-back (``a+a+a+``).

The heuristic is conservative on purpose. It rejects clearly-bad patterns;
it does not pretend to detect every pathological case. Connector authors
who hit a false positive can rewrite the pattern (typically by anchoring
or making quantifiers possessive). Authors who write subtly-bad patterns
that slip past the heuristic are caught by the perf gate in
``tests/perf/test_registry_perf.py`` (P99 < 100 µs on adversarial inputs).
"""

from __future__ import annotations

import re
from typing import Final

try:
    # The current google-re2 PyPI distribution ships a top-level `re2`
    # module, not a `google.re2` namespace package — a prior version of this
    # guard imported `google.re2`, which always raised ImportError even with
    # google-re2 installed. That bug, not a missing dependency, is why the
    # authoritative path was never active (issue #80).
    import re2 as _re2  # type: ignore[import-not-found,import-untyped]

    _HAS_RE2 = True
except ImportError:
    _HAS_RE2 = False


# A single quantifier token with no upper bound: `+`, `*`, or a brace
# quantifier whose upper bound is open (`{n,}`). `{n,m}` (explicit upper
# bound) is intentionally excluded — it can't grow backtracking cost
# unboundedly. `{1,}` is semantically identical to `+` and was a confirmed
# heuristic bypass (issue #80): the previous version of this scan only
# looked for the literal characters `+`/`*`.
_UNBOUNDED_QUANT: Final[str] = r"(?:[+*]|\{\d*,\})"

# Heuristic patterns indicating a likely catastrophic-backtracking risk:
#   * a group containing an unbounded quantifier, itself followed by
#     another unbounded quantifier — the canonical ``(a+)+`` shape. This
#     literal scan is blind past one level of nesting (see
#     ``_has_nested_unbounded_quantifier`` below for the general case).
#   * an alternation of identical branches inside an unbounded quantifier —
#     the ``(a|a)*`` shape.
#   * the same atom (a literal char, an escaped char, or a bracket class)
#     individually unbounded-quantified 3+ times back-to-back with no
#     separator — the ``a+a+a+`` shape. Each adjacent pair already adds a
#     polynomial degree of backtracking ambiguity; no legitimate slug
#     pattern needs a bare atom's quantifier repeated like this.
# All three are conservative; false positives are preferred over false
# negatives.
_NESTED_QUANTIFIER: Final[re.Pattern[str]] = re.compile(
    rf"\([^)]*{_UNBOUNDED_QUANT}[^)]*\)\s*{_UNBOUNDED_QUANT}"
)
_DUPLICATE_ALTERNATION: Final[re.Pattern[str]] = re.compile(
    rf"\(([^)|]+)\|\1\)\s*{_UNBOUNDED_QUANT}"
)
_ADJACENT_QUANTIFIED_ATOM: Final[re.Pattern[str]] = re.compile(
    rf"(\\.|\[[^\]]*\]|.){_UNBOUNDED_QUANT}(?:\1{_UNBOUNDED_QUANT}){{2,}}"
)


def _has_nested_unbounded_quantifier(src: str) -> bool:
    """True if some ``(...)`` group contains an unbounded quantifier
    anywhere in its body (at any nesting depth) and is itself immediately
    followed by another unbounded quantifier — the general ``(X+)+`` shape.

    ``_NESTED_QUANTIFIER`` is a fast literal scan but is blind past one
    level of nesting: for ``([a-z]+(?:x)?)+`` its ``[^)]*`` can't cross the
    inner ``)`` from ``(?:x)``, so the outer group's trailing ``+`` never
    gets linked back to the ``[a-z]+`` inside — a confirmed bypass (issue
    #80). Stdlib ``re`` has no recursive-matching support, so this is a
    small hand-rolled paren-depth scanner instead of a single regex.
    """
    depth_stack: list[int] = []
    in_class = False
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            continue
        if ch == "(":
            depth_stack.append(i)
        elif ch == ")":
            if depth_stack:
                start = depth_stack.pop()
                body = src[start + 1 : i]
                after = src[i + 1 :]
                if re.match(_UNBOUNDED_QUANT, after) and re.search(_UNBOUNDED_QUANT, body):
                    return True
        i += 1
    return False


def _heuristic_unsafe(src: str) -> bool:
    """Static ReDoS heuristic used when ``re2`` isn't installed.

    Factored out of :func:`assert_safe` so it can be unit-tested directly.
    ``assert_safe``'s branch selection depends on whether ``re2`` happens to
    be importable in the current environment — testing only through
    ``assert_safe`` would make these cases silently no-op wherever ``re2``
    is installed (e.g. CI, once the authoritative check is wired in via the
    ``dev`` extra).
    """
    return bool(
        _NESTED_QUANTIFIER.search(src)
        or _DUPLICATE_ALTERNATION.search(src)
        or _ADJACENT_QUANTIFIED_ATOM.search(src)
        or _has_nested_unbounded_quantifier(src)
    )


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

    if _heuristic_unsafe(src):
        raise ValueError(
            f"Pattern {src!r} has nested unbounded quantifiers, duplicate "
            f"alternation branches, or adjacent unbounded-quantified atoms, "
            f"and is rejected to prevent catastrophic backtracking on "
            f"adversarial input. Rewrite the pattern (anchor it, use "
            f"non-capturing groups, or make quantifiers possessive) "
            f"or install google-re2 for an authoritative linear-time check."
        )


def has_re2() -> bool:
    """Return ``True`` if ``google-re2`` is installed and active.

    Exposed for tests and observability — the registry doesn't change
    behavior based on this; ``assert_safe`` already chose the strategy.
    """
    return _HAS_RE2
