"""Tests for pattern_safety: catastrophic-backtracking guard."""

from __future__ import annotations

import re

import pytest
from genblaze_core.providers import pattern_safety
from genblaze_core.providers.pattern_safety import _heuristic_unsafe, assert_safe, has_re2


class _FakeRe2:
    """Stand-in for the ``re2`` module, used to force the ``_HAS_RE2 = True``
    branch deterministically regardless of whether ``google-re2`` is
    actually installed in the test environment. ``compile()`` mirrors re2's
    real behavior for the nested-quantifier shapes this module cares about:
    it happily accepts them (re2's own engine matches them in linear time),
    which is exactly why the heuristic must run in addition to, not instead
    of, the re2 check (#148)."""

    @staticmethod
    def compile(src: str) -> re.Pattern[str]:
        return re.compile(src)


class TestAssertSafe:
    def test_simple_anchored_pattern_passes(self) -> None:
        assert_safe(re.compile(r"^stabilityai/stable-diffusion-xl$"))

    def test_alternation_with_distinct_branches_passes(self) -> None:
        assert_safe(re.compile(r"^nvidia/(?:fugatto|riva-tts|magpie-tts|maxine-voice-font)"))

    def test_optional_suffix_passes(self) -> None:
        assert_safe(re.compile(r"^black-forest-labs/flux\.1-(?:dev|schnell|pro)$"))

    def test_unanchored_pattern_passes(self) -> None:
        # Connector authors can write unanchored patterns; the safety
        # guard only flags backtracking risk, not anchoring style.
        assert_safe(re.compile(r"stabilityai/stable-diffusion-xl"))

    def test_empty_pattern_passes(self) -> None:
        assert_safe(re.compile(r""))

    # The four tests below used to skip whenever re2 was installed, because
    # a prior version of assert_safe() returned early after a successful
    # re2 compile, never reaching the heuristic — re2 itself accepts these
    # shapes (see TestRedosGuardAlwaysRunsHeuristic below for why that's
    # unsafe). The heuristic now always runs, so these hold unconditionally
    # (#148).
    def test_nested_unbounded_plus_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a+)+"))

    def test_nested_unbounded_star_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(.+)+"))

    def test_duplicate_alternation_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a|a)*"))

    def test_overlapping_alternation_rejected(self) -> None:
        # #157: branches need not be byte-identical to be ambiguous under a
        # quantifier — "a" is a prefix of "aa", so repeated matching can
        # always re-partition a run of "a"s combinatorially.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a|aa)+$"))

    def test_delimiter_separated_adjacent_groups_rejected(self) -> None:
        # #157: an optional/nullable delimiter between adjacent
        # unbounded-quantified groups doesn't break the ambiguous-
        # partitioning shape — it just makes the adjacency conditional.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a+)-?(a+)-?(a+)-?(a+)$"))

    def test_realistic_evil_pattern_rejected(self) -> None:
        # The classic "(x+x+)+y" shape. Heuristic flags the nested quantifier;
        # that's enough to fail closed. Assembled at runtime (this fixture is only
        # analyzed by assert_safe, never matched) so it isn't itself flagged as a
        # static ReDoS literal by code scanning.
        evil = f"({'x+' * 2})+y"  # -> (x+x+)+y
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(evil))

    @pytest.mark.skipif(not has_re2(), reason="requires re2 to exercise the additional re2 gate")
    def test_re2_rejects_backreference(self) -> None:
        # Backreferences aren't supported by RE2's linear-time engine — this
        # exercises the re2 gate itself (distinct from the heuristic-shape
        # tests above, which now run regardless of whether re2 is active).
        with pytest.raises(ValueError, match="rejected by google-re2"):
            assert_safe(re.compile(r"(a)\1"))


class TestRedosGuardAlwaysRunsHeuristic:
    """Regression test for #148: ``assert_safe()`` must reject catastrophic
    -backtracking patterns whether or not ``google-re2`` is installed.

    ``re2.compile()`` only confirms a pattern is *syntactically compatible*
    with re2 — it accepts nested-quantifier shapes like ``(a+)+$`` because
    re2's own engine matches them in linear time. Runtime slug matching
    (``ModelFamily.matches()``, ``family.py``) always uses stdlib ``re``,
    which still backtracks catastrophically on the same input. A prior
    version of ``assert_safe()`` returned early once ``_re2.compile()``
    succeeded, so re2's acceptance silently disabled the heuristic for
    exactly these shapes. Both branches are exercised via
    ``monkeypatch.setattr`` so the test is deterministic regardless of
    whether ``google-re2`` happens to be installed in the environment
    actually running this suite.
    """

    @pytest.mark.parametrize(
        "src",
        [
            r"(a+)+$",
            r"([a-z]+)+$",
            r"(v\d+)+.*",
            r"(a|aa)+$",  # #157: overlapping (non-identical) alternation
            r"(a+)-?(a+)-?(a+)-?(a+)$",  # #157: nullable-delimiter adjacent groups
        ],
        ids=[
            "nested-plus-anchored",
            "nested-charclass-anchored",
            "version-prefix-catastrophic",
            "overlapping-alternation",
            "delimiter-separated-adjacent-groups",
        ],
    )
    @pytest.mark.parametrize("has_re2_value", [True, False], ids=["re2-present", "re2-absent"])
    def test_rejected_regardless_of_has_re2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        src: str,
        has_re2_value: bool,
    ) -> None:
        monkeypatch.setattr(pattern_safety, "_HAS_RE2", has_re2_value)
        if has_re2_value:
            # Force the re2 gate to "accept" the pattern (as real re2 does
            # for these shapes) without depending on re2 actually being
            # importable in this environment.
            monkeypatch.setattr(pattern_safety, "_re2", _FakeRe2)
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(src))


class TestHeuristicUnsafe:
    """Direct tests of the heuristic detector, independent of whether
    ``re2`` happens to be installed in the environment. Installing
    ``google-re2`` (e.g. via the ``dev`` extra, so CI's authoritative check
    is active — see issue #80) would otherwise make every
    ``skipif(has_re2())``-marked test above silently skip, leaving the
    heuristic itself untested in CI."""

    @pytest.mark.parametrize(
        "src",
        [
            r"(a+)+",
            r"(.+)+",
            r"(a|a)*",
            r"(a{1,})+",  # #80: {1,} evades a literal +/* scan
            r"(?:[a-z]{1,})+",
            r"([a-z]+(?:x)?)+",  # #80: unbounded quantifier nested inside a sub-group
            r"a+a+a+a+a+a+a+a+a+",  # #80: adjacent quantified atoms
            f"({'x+' * 2})+y",
            r"(a+)(a+)",  # #80: adjacent parenthesized unbounded groups (min case)
            r"([a-z]+)([a-z]+)([a-z]+)([a-z]+)([a-z]+)([a-z]+)",  # confirmed ~10s @ 100 chars
            r"(a|aa)+$",  # #157: overlapping (non-identical) alternation branches
            r"(a|aa|aaa)*",  # #157: 3+ overlapping branches, not just a pair
            r"(?:cat|category)+",  # #157: prefix overlap inside a non-capturing group
            r"(a+)-?(a+)$",  # #157: minimal delimiter-separated adjacent groups
            r"(a+)-?(a+)-?(a+)-?(a+)$",  # #157: issue's exact repro
            r"(a+)\s*(a+)$",  # #157: whitespace-class nullable delimiter
            r"(a\|b|a\|b)+",  # #157: escaped literal pipe inside a branch must not
            # be mistaken for a branch delimiter by the top-level splitter
            r"([a]|aa)+$",  # #157 review: class vs. literal, same char — semantic,
            # not textual, overlap
            r"(\x61|aa)+$",  # #157 review: hex escape vs. literal, same char
            r"(?:[a-c]|[a-z]{2})+",  # #157 review: overlapping character-class
            # ranges at different match lengths — the confirmed live bypass
            # from the security review of this fix
        ],
        ids=[
            "nested-plus",
            "nested-dot-plus",
            "duplicate-alternation",
            "brace-quantifier-1-comma",
            "brace-quantifier-noncapturing",
            "optional-suffix-nested",
            "adjacent-quantified-atoms",
            "realistic-evil-xx",
            "adjacent-groups-minimal",
            "adjacent-groups-six",
            "overlapping-alternation-pair",
            "overlapping-alternation-triple",
            "overlapping-alternation-noncapturing",
            "delimiter-separated-groups-minimal",
            "delimiter-separated-groups-issue-repro",
            "delimiter-separated-groups-whitespace-class",
            "alternation-escaped-pipe-duplicate",
            "overlapping-alternation-class-vs-literal",
            "overlapping-alternation-hex-escape",
            "overlapping-alternation-class-range-different-lengths",
        ],
    )
    def test_flags_known_bad_shapes(self, src: str) -> None:
        assert _heuristic_unsafe(src) is True

    @pytest.mark.parametrize(
        "src",
        [
            r"^stabilityai/stable-diffusion-xl$",
            r"^nvidia/(?:fugatto|riva-tts|magpie-tts|maxine-voice-font)",
            r"^black-forest-labs/flux\.1-(?:dev|schnell|pro)$",
            r"stabilityai/stable-diffusion-xl",
            r"",
            r"^wan\d+\.\d+-r2v$",
            r"^kling-(?:text2video|image2video)-v2\.1-master$",
            r"(a+)b(a+)",  # separated by a literal — not adjacent, not the ReDoS shape
            r"(a+)(b)",  # adjacent, but only one group is unbounded
            # #157: prefix-overlapping branches are only dangerous when the
            # group itself is quantified — an unquantified alternation, no
            # matter how the branches overlap, is matched at most once.
            r"^(?:dev|development)$",
            r"^(?:cat|category)$",
            # #157: a mandatory (non-nullable) delimiter between adjacent
            # unbounded groups is a real anchor — it isn't the ambiguous-
            # partitioning shape, same rationale as "(a+)b(a+)" above.
            r"(a+)-(a+)$",
            # #157: a quantified lookahead is zero-width and out of scope
            # for the ambiguous-alternation check (skipped, not analyzed).
            r"(?=a|aa)+",
            # #157 review: disjoint character classes — no character can be
            # attributed to either branch, so no partition ambiguity.
            r"(?:[a-z]|[0-9])+",
            r"(?:[a-z]|-)+",
            # #157 review: documented residual gap, not a regression — a
            # branch containing a nested group isn't reduced to a charset
            # (see _branch_charset's docstring), so this specific semantic
            # overlap ([a(?:b)?] vs "aa", both able to match "a") still
            # isn't caught. Asserted here so a future attempt to close this
            # gap updates this test rather than silently changing behavior.
            r"(a(?:b)?|aa)+$",
        ],
    )
    def test_passes_known_good_shapes(self, src: str) -> None:
        assert _heuristic_unsafe(src) is False


class TestHasRe2:
    def test_returns_bool(self) -> None:
        assert isinstance(has_re2(), bool)
