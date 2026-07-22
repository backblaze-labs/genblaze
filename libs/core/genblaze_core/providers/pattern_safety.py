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

Known residual gaps (not detected):

* An alternation branch containing a nested group (``a(?:b)?`` vs. ``aa``)
  isn't reduced to a character set — ``_branch_charset`` bails out on any
  ``(``/``)``/``|`` inside a branch, so overlap between two such branches
  falls back to the plain textual-prefix check, which won't catch this
  case. Closing this fully means recursing into sub-groups (and their own
  possible alternation) to compute a charset, i.e. re-implementing
  regex-language equivalence — disproportionate for a lightweight static
  check, so it's left as a documented gap rather than chased.
* A mandatory (non-nullable) delimiter whose own characters overlap with a
  flanking group's quantified content (e.g. ``(a+)a(a+)`` — the separator
  "a" isn't nullable, so ``_is_nullable_separator`` doesn't flag it, but it
  can still coincide with the groups' own matched text).
* Backreferences (``(a+)\1+``) aren't modeled by the static heuristic at
  all — no check here looks past a backreference to the group it repeats.
  Today this is masked whenever ``google-re2`` is installed, since re2
  rejects backreferences outright as an unsupported construct (a different
  gate than this heuristic — see above); without the ``re2``/``dev``
  extra, a backreference-shaped pattern has no heuristic coverage. Not
  introduced by this change; pre-existing and out of scope for #157.

All three are true ReDoS shapes in principle; they're narrower than the
patterns this module actively detects, and are left as future hardening
rather than blocking this heuristic on a full regex-language-equivalence
analysis, which is out of scope for a lightweight static check.
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
    except (re.error, OverflowError):
        # re.error: the fragment isn't valid as a standalone pattern (e.g.
        # it contains a backreference to a group defined outside it).
        # OverflowError: an absurdly large `{n,m}` repeat count (e.g.
        # `{0,4294967296}`) raises this instead of re.error. Either way we
        # can't prove the fragment is nullable, but per this module's
        # conservative bias (false positives over false negatives — see
        # module docstring), don't rule it out either: treat it as a
        # nullable/unsafe separator.
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


#: Character sets for the shorthand escape classes that expand to a concrete,
#: bounded alphabet. The negated forms (``\D``, ``\W``, ``\S``) are each
#: "everything except a known set" — rather than compute the true (effectively
#: unbounded, non-ASCII-aware) complement, ``_escape_charset`` treats them as
#: unknown/full (see its docstring).
_ESCAPE_CLASS_CHARS: Final[dict[str, str]] = {
    "d": "0123456789",
    "w": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    "s": " \t\n\r\f\v",
}


def _escape_charset(escape_body: str) -> frozenset[str] | None:
    """Return the set of characters an escape sequence matches, given the
    text right after its backslash (``"d"`` for ``\\d``, ``"x61"`` for
    ``\\x61``, ``"."`` for ``\\.``, etc.), or ``None`` (unknown/full,
    meaning "treat as potentially overlapping with anything") when it can't
    be resolved to a concrete bounded set.
    """
    if len(escape_body) == 1 and escape_body.lower() in _ESCAPE_CLASS_CHARS:
        # Uppercase (\D, \W, \S) is the negated class — unknown/full.
        return None if escape_body.isupper() else frozenset(_ESCAPE_CLASS_CHARS[escape_body])
    if len(escape_body) == 3 and escape_body[0] == "x":
        try:
            return frozenset([chr(int(escape_body[1:], 16))])
        except ValueError:
            return None
    if len(escape_body) == 1:
        return frozenset([escape_body])  # a literal char escaped for safety, e.g. \., \-, \(
    return None


def _consume_escape(src: str, i: int) -> tuple[frozenset[str] | None, int]:
    """Parse the escape sequence at ``src[i] == "\\\\"``. Returns its
    charset (see :func:`_escape_charset`) and the index just past it.
    """
    if i + 1 >= len(src):
        return None, i + 1
    if src[i + 1] == "x" and i + 3 < len(src):
        return _escape_charset(src[i + 1 : i + 4]), i + 4
    return _escape_charset(src[i + 1]), i + 2


def _bracket_charset(body: str) -> frozenset[str] | None:
    """Return the set of characters a ``[...]`` class body (the text
    between the brackets) matches, or ``None`` (unknown/full) for a negated
    class (``[^...]``) or a range too unusual to expand confidently.
    """
    if body.startswith("^"):
        return None
    chars: set[str] = set()
    i = 0
    n = len(body)
    while i < n:
        if body[i] == "\\":
            escaped, i = _consume_escape(body, i)
            if escaped is None:
                return None
            chars.update(escaped)
            continue
        # A range like `a-z`: literal `-` not at the start/end of the class
        # and not immediately after an escape (already consumed above).
        if i + 2 < n and body[i + 1] == "-" and body[i + 2] != "]":
            lo, hi = body[i], body[i + 2]
            if ord(lo) > ord(hi) or ord(hi) - ord(lo) > 256:
                return None  # malformed or suspiciously large — don't expand
            chars.update(chr(c) for c in range(ord(lo), ord(hi) + 1))
            i += 3
            continue
        chars.add(body[i])
        i += 1
    return frozenset(chars)


def _branch_charset(branch: str) -> tuple[bool, frozenset[str] | None]:
    """Return ``(reducible, charset)`` for ``branch``.

    ``reducible`` is ``False`` if ``branch`` contains a nested group or an
    atom with a genuinely unbounded quantifier — i.e. it isn't a flat
    sequence of simple atoms (literal chars, escapes, bracket classes,
    each with at most a *bounded* quantifier: ``?``, ``{n}``, ``{n,m}``)
    this function can characterize. :func:`_has_ambiguous_quantified_alternation`
    skips the semantic overlap check for such branches and relies on its
    plain textual prefix check instead — catching byte-identical/prefix
    overlaps for that shape, but not semantic ones. Recursing into nested
    groups to close that gap fully would mean re-implementing regex-language
    equivalence, disproportionate for this lightweight heuristic (see the
    module's "Known residual gaps").

    When ``reducible`` is ``True``, ``charset`` is the set of characters
    the branch can match anywhere in its length, or ``None`` (unknown/full
    — treated as overlapping with anything) when some atom couldn't be
    resolved to a concrete set (e.g. a negated class like ``\\D``).
    """
    charset: set[str] = set()
    unknown = False
    i = 0
    n = len(branch)
    while i < n:
        ch = branch[i]
        if ch in "(|)":
            return False, None
        if ch == "\\":
            atom_charset, i = _consume_escape(branch, i)
        elif ch == "[":
            close = branch.find("]", i + 1)
            if close == -1:
                return False, None
            atom_charset = _bracket_charset(branch[i + 1 : close])
            i = close + 1
        else:
            atom_charset = frozenset(ch)
            i += 1
        if atom_charset is None:
            unknown = True
        else:
            charset.update(atom_charset)

        # Skip a bounded quantifier on the atom just consumed — its exact
        # repeat count doesn't matter for a character-overlap check, only
        # that it IS bounded (an unbounded one still bails out below, since
        # that shape is already handled by the other heuristic checks and
        # complicates length reasoning this function doesn't attempt).
        quant = re.match(r"\?|\{(\d*),?(\d*)\}", branch[i:])
        if quant:
            if quant.group() != "?" and (
                not quant.group(1) or ("," in quant.group() and not quant.group(2))
            ):
                return False, None  # `{,m}` / `{n,}` — unbounded
            i += quant.end()

    return True, (None if unknown else frozenset(charset))


def _charsets_overlap(a: frozenset[str] | None, b: frozenset[str] | None) -> bool:
    """True if two atom charsets could match the same character. ``None``
    (unknown/full — see :func:`_escape_charset`/:func:`_bracket_charset`) is
    treated as overlapping with anything, per this module's bias toward
    false positives over false negatives.
    """
    return a is None or b is None or bool(a & b)


def _has_ambiguous_quantified_alternation(src: str) -> bool:
    """True if some ``(...)`` group is a 2+-branch alternation where two
    branches can match overlapping text, and the group itself is
    immediately followed by an unbounded quantifier — the ``(a|aa)+`` shape.

    A quantified group doesn't need byte-identical branches to be ambiguous
    under repetition — any two branches that can match some of the same
    characters give the backtracker multiple equivalent ways to attribute a
    run of those characters to one branch or the other (issue #157 — the
    previous check matched only literally-identical branches). Two
    complementary checks catch this:

    * A textual prefix relationship (``a`` / ``aa``) — cheap and catches
      the byte-identical case plus any branch that's literally a prefix of
      another, regardless of internal structure (nested groups included).
    * For branches reducible to a flat sequence of simple atoms (no nested
      groups — see :func:`_branch_charset`), a *semantic* check: their
      matched-character sets overlap at all (:func:`_charsets_overlap`).
      This catches shapes the textual check misses because the overlap
      isn't a literal prefix, e.g. ``[a-c]`` vs. ``[a-z]{2}`` (overlapping
      character ranges, different-length matches) or ``[a]`` vs. ``aa``
      (a class and a literal expressing the same character differently).

    Supersedes the old ``_DUPLICATE_ALTERNATION`` regex (2-branch,
    byte-identical, no-nesting-in-branch only): walking
    ``_iter_group_spans`` covers 3+ branches and alternations containing
    nested groups for the textual check, and ``_branch_charset`` adds
    semantic overlap for branches without nested groups.

    Scoped to plain and non-capturing groups. Lookaround groups (``(?=...)``,
    ``(?!...)``, ``(?<=...)``, ``(?<!...)``) are zero-width and quantifying
    one is unusual; skipped rather than risk misreading unfamiliar syntax.
    An empty branch (``(a|)+``) is also skipped — Python's ``re`` special-
    cases zero-width alternatives in a loop to avoid infinite looping, so it
    isn't the same exponential-backtracking shape as a genuine overlap.
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
                reducible_a, charset_a = _branch_charset(branch_a)
                reducible_b, charset_b = _branch_charset(branch_b)
                if reducible_a and reducible_b and _charsets_overlap(charset_a, charset_b):
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
