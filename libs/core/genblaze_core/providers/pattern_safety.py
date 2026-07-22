"""Reject regex patterns prone to catastrophic backtracking.

A ``ModelFamily``'s pattern is a public surface — connector authors write it
once at module load, and every slug a user submits is matched against it
during ``Pipeline.run()`` preflight. A poorly-written pattern with nested
unbounded quantifiers (``(a+)+``, ``(.+)+``, ``(a|a)*``) on adversarial
input can take seconds to match a single string, turning preflight into a
DoS vector.

This module guards against that at ``ModelFamily`` construction time —
patterns that fail the safety check raise ``ValueError`` during connector
import, before any user code runs. Runtime slug matching
(``ModelFamily.matches()``, ``family.py``) always matches with a stdlib
``re.Pattern``, never ``re2`` — so the static heuristic below always runs,
regardless of whether ``re2`` is installed. Two gates are applied:

* If ``google-re2`` is installed, every pattern is ALSO recompiled through
  it as an additional gate: ``re2`` rejects constructs its own engine can't
  support (e.g. backreferences) outright. This is *not* a substitute for
  the heuristic — ``re2`` accepts nested-quantifier shapes like ``(a+)+``
  because *its own* linear-time engine matches them fine, but stdlib
  ``re`` (the engine actually used at match time) still backtracks
  catastrophically on the same input. A prior version of this guard
  returned early once the re2 compile succeeded, silently disabling the
  heuristic for exactly these shapes whenever re2 was installed (#148).
  Install re2 via the ``re2`` extra (``pip install "genblaze-core[re2]"``)
  or the ``dev`` extra, which includes it — CI installs ``libs/core[dev]``,
  so this path is active there by default.
* The static heuristic always runs and flags the most common
  catastrophic-backtracking shapes: nested unbounded quantifiers (bare
  ``+``/``*`` or an open-ended ``{n,}`` brace quantifier, at any nesting
  depth), a quantified alternation with overlapping branches — not just
  byte-identical ones, e.g. ``(a|aa)+`` (issue #157) — runs of the same
  atom quantified back-to-back (``a+a+a+``), and 2+ adjacent parenthesized
  groups each carrying an unbounded quantifier, optionally separated by a
  nullable delimiter such as ``-?`` or ``\\s*`` (``(a+)(a+)``,
  ``(a+)-?(a+)`` — issue #157) — the ambiguous-partitioning shape, distinct
  from ``(a+)+``'s outer-quantifier shape.

The heuristic is conservative on purpose. It rejects clearly-bad patterns;
it does not pretend to detect every pathological case. Connector authors
who hit a false positive can rewrite the pattern (typically by anchoring
or making quantifiers possessive). Authors who write subtly-bad patterns
that slip past the heuristic are caught by the perf gate in
``tests/perf/test_registry_perf.py`` (P99 < 100 µs on adversarial inputs).

Known residual gaps (not detected): overlapping alternation branches where
neither is a prefix of the other (e.g. shared suffix or interior overlap,
as opposed to ``a``/``aa``'s prefix relationship); a mandatory (non-
nullable) delimiter whose own characters overlap with a flanking group's
quantified content (e.g. ``(a+)a(a+)`` — the separator "a" isn't nullable,
so ``_is_nullable_separator`` doesn't flag it, but it can still coincide
with the groups' own matched text). Both are true ReDoS shapes in
principle; they're narrower and rarer than the patterns above, and are left
as future hardening rather than blocking this heuristic on a full
regex-language-equivalence analysis, which is out of scope for a
lightweight static check.
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
_ADJACENT_QUANTIFIED_ATOM: Final[re.Pattern[str]] = re.compile(
    rf"(\\.|\[[^\]]*\]|.){_UNBOUNDED_QUANT}(?:\1{_UNBOUNDED_QUANT}){{2,}}"
)

# A leading capturing-group marker to strip before splitting an alternation's
# branches: non-capturing (`?:`) or named (`?P<name>`). Lookaround markers
# (`?=`, `?!`, `?<=`, `?<!`) intentionally don't match here — see
# ``_has_ambiguous_quantified_alternation``.
_GROUP_MARKER: Final[re.Pattern[str]] = re.compile(r"^\?(?::|P<[^>]*>)")


def _iter_group_spans(src: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` index pairs for each ``(...)`` group in
    ``src``, matching each close paren to its own open paren so nesting is
    handled correctly. Skips escaped parens (``\\(``, ``\\)``) and parens
    inside a bracket class (``[()]`` — literal there, not grouping).

    Stdlib ``re`` has no recursive-matching support, so callers that need
    to reason about paren *nesting* (as opposed to a flat text scan) use
    this small hand-rolled scanner instead of a single regex.
    """
    spans: list[tuple[int, int]] = []
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
                spans.append((depth_stack.pop(), i))
        i += 1
    return spans


def _has_nested_unbounded_quantifier(src: str) -> bool:
    """True if some ``(...)`` group contains an unbounded quantifier
    anywhere in its body (at any nesting depth) and is itself immediately
    followed by another unbounded quantifier — the general ``(X+)+`` shape.

    ``_NESTED_QUANTIFIER`` is a fast literal scan but is blind past one
    level of nesting: for ``([a-z]+(?:x)?)+`` its ``[^)]*`` can't cross the
    inner ``)`` from ``(?:x)``, so the outer group's trailing ``+`` never
    gets linked back to the ``[a-z]+`` inside — a confirmed bypass (issue
    #80).
    """
    for start, end in _iter_group_spans(src):
        body = src[start + 1 : end]
        after = src[end + 1 :]
        if re.match(_UNBOUNDED_QUANT, after) and re.search(_UNBOUNDED_QUANT, body):
            return True
    return False


def _is_nullable_separator(fragment: str) -> bool:
    """True if ``fragment`` — the raw text sitting between two top-level
    groups — can itself match the empty string, e.g. ``-?``, ``\\s*``,
    ``(?:foo)?``, or the empty string itself.

    A separator that can vanish doesn't break adjacency: on the adversarial
    input that makes the two flanking groups ambiguous, the regex engine
    also tries the zero-length match for the separator, so the groups are
    effectively adjacent on that backtracking path too (issue #157 — the
    original adjacency check required the separator to be literally empty,
    which a bare ``-?`` between ``(a+)`` groups evades while preserving the
    exponential blowup). Checked by actually compiling the fragment and
    matching it against ``""`` rather than pattern-matching its syntax,
    since that generalizes to any nullable construct without needing to
    enumerate them.
    """
    if fragment == "":
        return True
    try:
        return re.fullmatch(fragment, "") is not None
    except re.error:
        # The fragment isn't valid as a standalone pattern (e.g. it contains
        # a backreference to a group defined outside it). Can't prove it's
        # nullable, but per this module's conservative bias (false positives
        # over false negatives — see module docstring), don't rule it out
        # either: treat it as a nullable/unsafe separator.
        return True


def _has_adjacent_unbounded_groups(src: str) -> bool:
    """True if 2+ top-level ``(...)`` groups, each containing an unbounded
    quantifier, appear back-to-back — separated by nothing, or only by text
    that can match the empty string — the ``(a+)(a+)(a+)`` /
    ``(a+)-?(a+)-?(a+)`` shape.

    Unlike ``(a+)+`` (an outer quantifier repeating one ambiguous group),
    this has *no* outer quantifier at all — the ambiguity comes purely from
    every adjacent split point between the groups being a valid match, which
    is combinatorial in the number of groups. Confirmed empirically
    (catastrophic: 6 adjacent ``([a-z]+)`` groups took ~10s to match a
    100-character adversarial string under stdlib ``re``) — a real bypass
    of both ``_has_nested_unbounded_quantifier`` (no group is *itself*
    followed by a quantifier) and ``_ADJACENT_QUANTIFIED_ATOM`` (a
    parenthesized group isn't a single "atom" that regex matches). A
    *mandatory* (non-nullable) separator between the groups — a literal
    character the group's own quantifier can't also match away — remains a
    real anchor and isn't flagged (see ``_is_nullable_separator``).
    """
    spans = _iter_group_spans(src)
    # Only top-level groups matter for this shape — a group nested inside
    # another is already covered by _has_nested_unbounded_quantifier, and
    # its span isn't "adjacent" to a sibling in the sense that matters here.
    top_level = [s for s in spans if not any(o[0] < s[0] and s[1] < o[1] for o in spans)]
    top_level.sort()

    run = 0
    prev_end: int | None = None
    for start, end in top_level:
        unbounded = bool(re.search(_UNBOUNDED_QUANT, src[start + 1 : end]))
        adjacent = prev_end is not None and _is_nullable_separator(src[prev_end + 1 : start])
        run = run + 1 if (adjacent and unbounded and run) else int(unbounded)
        if run >= 2:
            return True
        prev_end = end
    return False


def _split_top_level_alternatives(body: str) -> list[str]:
    """Split ``body`` on ``|`` characters at nesting depth 0, treating
    nested ``(...)`` groups and ``[...]`` classes as opaque so a branch
    boundary inside a nested alternation (``(?:a|(?:b|c))``) isn't mistaken
    for one of ``body``'s own top-level branches.
    """
    branches: list[str] = []
    depth = 0
    in_class = False
    start = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
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
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            branches.append(body[start:i])
            start = i + 1
        i += 1
    branches.append(body[start:])
    return branches


def _has_ambiguous_quantified_alternation(src: str) -> bool:
    """True if some ``(...)`` group is a 2+-branch alternation where one
    branch is a prefix of another (including byte-identical branches), and
    the group itself is immediately followed by an unbounded quantifier —
    the ``(a|aa)+`` shape.

    A quantified group doesn't need byte-identical branches to be
    ambiguous under repetition: a strict prefix relationship (``a`` /
    ``aa``) is just as dangerous, since a run of the shared prefix
    character(s) can always be re-partitioned as one long branch match or
    several short ones, giving the backtracker exponentially many
    equivalent splits (issue #157 — the previous check matched only
    literally-identical branches). Supersedes the old ``_DUPLICATE_ALTERNATION``
    regex (2-branch, no-nesting-in-branch only): walking ``_iter_group_spans``
    also covers 3+ branches and alternations containing nested groups.

    Scoped to plain and non-capturing groups. Lookaround groups (``(?=...)``,
    ``(?!...)``, ``(?<=...)``, ``(?<!...)``) are zero-width and quantifying
    one is unusual; skipped rather than risk misreading unfamiliar syntax.
    An empty branch (``(a|)+``) is also skipped — Python's ``re`` special-
    cases zero-width alternatives in a loop to avoid infinite looping, so it
    isn't the same exponential-backtracking shape as a genuine prefix
    overlap.
    """
    for start, end in _iter_group_spans(src):
        if not re.match(_UNBOUNDED_QUANT, src[end + 1 :]):
            continue
        body = src[start + 1 : end]
        marker = _GROUP_MARKER.match(body)
        if body.startswith("?") and not marker:
            continue
        if marker:
            body = body[marker.end() :]
        if "|" not in body:
            continue
        branches = _split_top_level_alternatives(body)
        for i, branch_a in enumerate(branches):
            for branch_b in branches[i + 1 :]:
                if not branch_a or not branch_b:
                    continue
                if (
                    branch_a == branch_b
                    or branch_a.startswith(branch_b)
                    or branch_b.startswith(branch_a)
                ):
                    return True
    return False


def _heuristic_unsafe(src: str) -> bool:
    """Static ReDoS heuristic that always runs, whether or not ``re2`` is
    installed.

    Runtime slug matching (``ModelFamily.matches()``) always uses stdlib
    ``re``, never ``re2`` — so ``re2`` accepting a pattern says nothing
    about that pattern's safety under the engine actually used at match
    time (#148). Factored out of :func:`assert_safe` so it can be
    unit-tested directly regardless of whether ``re2`` happens to be
    importable in the current environment.
    """
    return bool(
        _NESTED_QUANTIFIER.search(src)
        or _ADJACENT_QUANTIFIED_ATOM.search(src)
        or _has_nested_unbounded_quantifier(src)
        or _has_adjacent_unbounded_groups(src)
        or _has_ambiguous_quantified_alternation(src)
    )


def assert_safe(pattern: re.Pattern[str]) -> None:
    """Raise ``ValueError`` if ``pattern`` looks unsafe to match.

    Call from ``ModelFamily.__post_init__`` so connector imports fail fast
    when a pattern would put preflight at risk under adversarial input.

    Two independent gates apply, both when ``re2`` is installed: the re2
    compile check catches constructs re2 itself can't support, and the
    heuristic below always runs regardless, because runtime matching
    (``ModelFamily.matches()``) uses stdlib ``re``, which re2's acceptance
    of a pattern says nothing about (#148).
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

    if _heuristic_unsafe(src):
        raise ValueError(
            f"Pattern {src!r} has nested unbounded quantifiers, an ambiguous "
            f"(overlapping or duplicate) alternation under a quantifier, "
            f"adjacent unbounded-quantified atoms, or adjacent (optionally "
            f"delimiter-separated) unbounded-quantified groups, and is "
            f"rejected to prevent catastrophic backtracking on adversarial "
            f"input. Rewrite the pattern (anchor it, use non-capturing "
            f"groups, or make quantifiers possessive) — installing "
            f"google-re2 does not bypass this check, since runtime matching "
            f"always uses stdlib re, which this heuristic protects."
        )


def has_re2() -> bool:
    """Return ``True`` if ``google-re2`` is installed and active.

    Exposed for tests and observability — the registry doesn't change
    behavior based on this; ``assert_safe`` runs the heuristic gate
    unconditionally and additionally runs the re2 gate when this is
    ``True``.
    """
    return _HAS_RE2
