"""Tests for pattern_safety: catastrophic-backtracking guard."""

from __future__ import annotations

import re

import pytest
from genblaze_core.providers import pattern_safety
from genblaze_core.providers.pattern_safety import (
    _bracket_charset,
    _branch_charset,
    _case_fold_charset,
    _escape_charset,
    _heuristic_unsafe,
    _is_nullable_separator,
    _unsafe_reason,
    assert_safe,
    has_re2,
)


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
        # always re-partition a run of "a"s combinatorially. Assembled at
        # runtime (only analyzed by assert_safe, never matched) so the
        # catastrophic literal isn't itself flagged by CodeQL's ReDoS scan.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(f"(a|{'a' * 2})+$"))

    def test_delimiter_separated_adjacent_groups_rejected(self) -> None:
        # #157: an optional/nullable delimiter between adjacent
        # unbounded-quantified groups doesn't break the ambiguous-
        # partitioning shape — it just makes the adjacency conditional.
        # Assembled at runtime so the fixture isn't flagged as a static
        # ReDoS literal by code scanning (see test_realistic_evil_pattern).
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(f"{'(a+)-?' * 3}(a+)$"))

    @pytest.mark.parametrize(
        "src",
        [
            r"(?:(?:a|aa))+$",  # #196: non-capturing redundant wrapper
            r"((a|aa))+$",  # #196: capturing redundant wrapper
        ],
    )
    def test_redundant_group_wrapper_around_alternation_rejected(self, src: str) -> None:
        # #196: a group wrapped redundantly around an already-ambiguous
        # alternation must not defeat the check the un-wrapped `(a|aa)+$`
        # already fails.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(src))

    def test_triple_nested_redundant_wrapper_rejected(self) -> None:
        # #196: _unwrap_redundant_group's docstring claims it repeats "until
        # no more wrappers remain" -- pin that beyond the single-level case.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(?:(?:(?:a|aa)))+$"))

    def test_disjoint_charset_nullable_separator_groups_allowed(self) -> None:
        # #200: flanking groups with disjoint charsets have an unambiguous
        # split point and are linear-time safe -- must not be over-rejected
        # as a regression of the #157 nullable-separator broadening.
        assert_safe(re.compile(r"([a-z]+)-?([0-9]+)"))

    def test_disjoint_charset_mandatory_separator_groups_allowed(self) -> None:
        assert_safe(re.compile(r"([a-z]+)-([0-9]+)"))

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
            r"(?:(?:a|aa))+$",  # #196: non-capturing redundant wrapper
            r"((a|aa))+$",  # #196: capturing redundant wrapper
        ],
        ids=[
            "nested-plus-anchored",
            "nested-charclass-anchored",
            "version-prefix-catastrophic",
            "overlapping-alternation",
            "delimiter-separated-adjacent-groups",
            "redundant-noncapturing-wrapper",
            "redundant-capturing-wrapper",
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


class TestSafePatternsAllowedRegardlessOfHasRe2:
    """Regression test for #200: a disjoint-charset, linear-time-safe
    pattern must be ALLOWED whether or not ``google-re2`` is installed,
    proving the #200 charset-overlap gate and the #196 stricter alternation
    check coexist without either shape regressing the other."""

    @pytest.mark.parametrize(
        "src",
        [
            r"([a-z]+)-?([0-9]+)",  # #200: disjoint charsets, nullable separator
            r"([a-z]+)-([0-9]+)",  # #200: disjoint charsets, mandatory separator
        ],
        ids=["disjoint-nullable-separator", "disjoint-mandatory-separator"],
    )
    @pytest.mark.parametrize("has_re2_value", [True, False], ids=["re2-present", "re2-absent"])
    def test_allowed_regardless_of_has_re2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        src: str,
        has_re2_value: bool,
    ) -> None:
        monkeypatch.setattr(pattern_safety, "_HAS_RE2", has_re2_value)
        if has_re2_value:
            monkeypatch.setattr(pattern_safety, "_re2", _FakeRe2)
        assert_safe(re.compile(src))  # must not raise


class TestAdjacentGroupsNullableMiddleGroup:
    """Regression tests for a #200-review finding: comparing only the
    immediately-preceding group's charset missed the case where a NULLABLE
    unbounded group sits between two others. `(a+)(b*)(a+)$` must stay
    rejected -- on the path where `b*` matches zero characters, it collapses
    to the canonical `(a+)(a+)$` ambiguous-partition shape, even though `a`
    and `b` are themselves disjoint charsets. `(a+)(b*)(c+)$`, where the
    outer groups are ALSO disjoint from each other, must stay allowed."""

    def test_nullable_middle_group_reopens_ambiguity_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a+)(b*)(a+)$"))

    def test_nullable_middle_group_all_disjoint_allowed(self) -> None:
        assert_safe(re.compile(r"(a+)(b*)(c+)$"))

    def test_two_nullable_groups_transitive_overlap_rejected(self) -> None:
        # Two nullable unbounded groups in a row before the charset repeats:
        # the union must keep accumulating across BOTH, not just the last one.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a+)(b*)(c*)(a+)$"))

    def test_mandatory_separator_before_nullable_group_severs_carry_allowed(self) -> None:
        # #200 re-review: a MANDATORY (non-nullable) separator right before a
        # later nullable group is a hard boundary -- it must reset what's
        # carried forward, not let an earlier group's charset silently union
        # through it. Before this fix, the stale carried charset from group 1
        # wrongly made this look ambiguous with group 3.
        assert_safe(re.compile(r"(a+)-(b*)(a+)$"))

    def test_mandatory_content_before_nullable_group_severs_carry_allowed(self) -> None:
        # Same "hard boundary" rule, but the boundary is the PRECEDING
        # group's own mandatory content rather than a literal separator.
        assert_safe(re.compile(r"(a+)(c+)(b*)(a+)$"))


class TestIgnoreCaseAwareCharsetOverlap:
    """Regression tests for a security-review finding on the #200 fix: the
    charset-overlap gates (adjacent groups AND quantified alternation) must
    fold case when the compiled pattern has ``re.IGNORECASE`` set, or a
    pattern whose groups are disjoint only in literal source text (e.g.
    `[a-z]` vs. `[A-Z]`) slips past as a false "safe" verdict despite
    matching identical characters at runtime."""

    def test_adjacent_groups_ignorecase_folds_to_overlap_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(
                re.compile(r"([a-z]+)([A-Z]+)([a-z]+)([A-Z]+)([a-z]+)([A-Z]+)$", re.IGNORECASE)
            )

    def test_adjacent_groups_disjoint_case_without_ignorecase_allowed(self) -> None:
        # Same shape, no flag -- genuinely disjoint at match time, must stay safe.
        assert_safe(re.compile(r"([a-z]+)([A-Z]+)([a-z]+)([A-Z]+)([a-z]+)([A-Z]+)$"))

    def test_alternation_ignorecase_folds_to_overlap_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(?:[a-z]|[A-Z])+", re.IGNORECASE))

    def test_alternation_disjoint_case_without_ignorecase_allowed(self) -> None:
        assert_safe(re.compile(r"(?:[a-z]|[A-Z])+"))

    def test_non_ascii_case_fold_with_multi_char_mapping_rejected(self) -> None:
        # Turkish dotted capital I (U+0130) lower-cases to a TWO-character
        # string ("i" + a combining dot above), unlike the ASCII a/A pairs
        # above. A naive fold that adds `ch.lower()` as one opaque element
        # (instead of decomposing it into its individual characters) never
        # produces the plain "i" needed to detect overlap with a literal
        # "i" elsewhere -- silently reopening this exact bypass class for
        # non-ASCII letters even after the ASCII case is fixed.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile("(İ+)(i+)(İ+)(i+)(İ+)(i+)$", re.IGNORECASE))


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
            r"(?:[]a-y]|qq)+$",  # #157 review round 2: a literal `]` as the
            # first char of a bracket class (real re semantics: `[]a-y]`
            # matches `]` or a-y) was mis-parsed as an empty class, silently
            # dropping the whole a-y range (including `q`) from the computed
            # charset — a confirmed false-negative bypass caught in re-review
            r"(?:(?:a|aa))+$",  # #196: redundant non-capturing wrapper around
            # an already-ambiguous alternation must not hide it
            r"((a|aa))+$",  # #196: redundant capturing wrapper, same shape
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
            "overlapping-alternation-leading-bracket-literal",
            "redundant-noncapturing-wrapper",
            "redundant-capturing-wrapper",
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
            # #157 follow-up: two unbounded groups on opposite sides of a
            # TOP-LEVEL `|` are in different alternation branches — never
            # matched on the same path, so no adjacent-partitioning ambiguity.
            # A regression here (treating `|` as a nullable separator) rejected
            # these linear-time patterns.
            r"(a+)|(b+)",
            r"(?:a+)|(?:b+)",
            r"^(openai/gpt-4\d*)|(anthropic/claude-\d+)$",
            # #157 review: documented residual gap, not a regression — a
            # branch containing a nested group isn't reduced to a charset
            # (see _branch_charset's docstring), so this specific semantic
            # overlap ([a(?:b)?] vs "aa", both able to match "a") still
            # isn't caught. Asserted here so a future attempt to close this
            # gap updates this test rather than silently changing behavior.
            r"(a(?:b)?|aa)+$",
            # #200: flanking unbounded groups with disjoint charsets have an
            # unambiguous split point and are linear-time safe -- must not
            # be over-rejected as a regression of the #157 nullable-
            # separator broadening.
            r"([a-z]+)-?([0-9]+)",
            r"([a-z]+)-([0-9]+)",
            # #196 review: documented residual gap, not a regression (the
            # pre-#196 heuristic missed these too) -- a redundant wrapper
            # that itself carries a no-op/optional quantifier isn't
            # unwrapped by _unwrap_redundant_group, which only strips a
            # wrapper whose entire body is exactly one more nested group
            # with nothing else. Asserted here so a future attempt to widen
            # the unwrap updates this test rather than silently changing
            # behavior (see the module's "Known residual gaps").
            r"((a|aa){1})+$",
            r"((a|aa)?)+$",
        ],
    )
    def test_passes_known_good_shapes(self, src: str) -> None:
        assert _heuristic_unsafe(src) is False

    @pytest.mark.parametrize("src", [r"(?:v1|v2)+", r"(?:ab|bc)+", r"(?:foo|far)+"])
    def test_conservative_over_rejection_is_intentional(self, src: str) -> None:
        """The charset-overlap check is a deliberate over-approximation: two
        branches sharing ANY character are flagged, so some non-ambiguous
        alternations (the differing char actually disambiguates them) are
        rejected too. This is intentional per the module's false-positive-over-
        false-negative bias — pinned here so a future attempt to tighten the
        check updates this test and the module docstring's "Deliberate
        over-rejection" note deliberately, not silently."""
        assert _heuristic_unsafe(src) is True


class TestUnsafeReason:
    """The rejection message names the specific shape and gives remediation
    that actually clears the heuristic (#157) — not a generic list including
    fixes that don't apply (e.g. making the quantifier possessive)."""

    def test_reason_none_for_safe_pattern(self) -> None:
        assert _unsafe_reason(r"^gpt-4o(?:-mini)?$") is None

    @pytest.mark.parametrize(
        ("src", "needle"),
        [
            (r"(a+)+", "nested unbounded"),
            (r"(a|aa)+", "overlapping"),
            (r"a+a+a+", "back-to-back"),
            (r"(a+)(a+)", "adjacent unbounded-quantified groups"),
        ],
    )
    def test_reason_names_the_specific_shape(self, src: str, needle: str) -> None:
        reason = _unsafe_reason(src)
        assert reason is not None and needle in reason

    def test_possessive_alternation_message_does_not_advise_possessive(self) -> None:
        """`(a|aa)++` is still (correctly) flagged — the possessive quantifier
        doesn't remove the branch overlap. The reason must point at the overlap,
        not send the author in a circle by advising possessive quantifiers.
        Checked via _unsafe_reason directly: assert_safe's re2 gate would reject
        the possessive `++` first in a re2-present env, for a different reason."""
        reason = _unsafe_reason(r"(a|aa)++")
        assert reason is not None
        assert "remove the overlap" in reason
        assert "possessive does not clear" in reason

    def test_assert_safe_message_names_the_shape(self) -> None:
        """End-to-end: `(a|aa)+` is accepted by re2 (linear-time) but flagged by
        the always-on heuristic, so assert_safe raises the shape-specific
        message rather than a generic catch-all."""
        with pytest.raises(ValueError, match="overlapping text") as exc:
            # assembled at runtime so the catastrophic literal isn't flagged
            # by CodeQL's ReDoS scan (see test_realistic_evil_pattern_rejected)
            assert_safe(re.compile(f"(a|{'a' * 2})+"))
        msg = str(exc.value)
        assert "remove the overlap" in msg
        # The old generic message listed every reason + fixes that don't apply.
        assert "nested unbounded quantifiers such as" not in msg


class TestParserHelpers:
    """Direct unit tests for the hand-rolled sub-parsers, which otherwise are
    only exercised end-to-end through the heuristic — a regression in one would
    surface as a subtle false positive/negative that's hard to localize."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("abc", frozenset("abc")),
            ("a-c", frozenset("abc")),
            (r"\d", frozenset("0123456789")),
            ("]a-c", frozenset("]abc")),  # leading ] is a literal member
        ],
    )
    def test_bracket_charset_concrete(self, body: str, expected: frozenset[str]) -> None:
        assert _bracket_charset(body) == expected

    def test_bracket_charset_negated_is_unknown(self) -> None:
        assert _bracket_charset("^a") is None  # negated class -> unknown/full

    @pytest.mark.parametrize(
        ("escape_body", "expected"),
        [("d", frozenset("0123456789")), ("x61", frozenset("a")), (".", frozenset("."))],
    )
    def test_escape_charset_concrete(self, escape_body: str, expected: frozenset[str]) -> None:
        assert _escape_charset(escape_body) == expected

    def test_escape_charset_negated_is_unknown(self) -> None:
        assert _escape_charset("D") is None  # \D is "everything except digits"

    @pytest.mark.parametrize(
        ("branch", "reducible", "charset"),
        [
            ("abc", True, frozenset("abc")),
            ("a?b", True, frozenset("ab")),  # bounded quantifier is fine
            ("a+", False, None),  # unbounded -> not reducible (regression guard)
            ("a*", False, None),
            ("(?:x)", False, None),  # nested group -> not reducible
            (r"\D", True, None),  # reducible but charset unknown/full
        ],
    )
    def test_branch_charset(
        self, branch: str, reducible: bool, charset: frozenset[str] | None
    ) -> None:
        assert _branch_charset(branch) == (reducible, charset)

    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            ("a??", frozenset("a")),  # lazy `?` after bounded `?` isn't a stray atom
            ("a{2,3}+", frozenset("a")),  # possessive modifier after `{n,m}`
        ],
    )
    def test_branch_charset_consumes_lazy_possessive_modifier(
        self, branch: str, expected: frozenset[str]
    ) -> None:
        # #196: a trailing lazy (`?`) or possessive (`+`) modifier right
        # after a bounded quantifier must be consumed, not treated as a new
        # literal atom -- before this fix, `_branch_charset('a??')` returned
        # `(True, frozenset({'a', '?'}))`, leaking a spurious `?`.
        assert _branch_charset(branch) == (True, expected)

    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            ("a+", frozenset("a")),
            ("[a-z]+", frozenset("abcdefghijklmnopqrstuvwxyz")),
            ("a{2,}", frozenset("a")),
        ],
    )
    def test_branch_charset_allow_unbounded(self, branch: str, expected: frozenset[str]) -> None:
        # #200: _has_adjacent_unbounded_groups needs the charset of an
        # unbounded group's body (to compare flanking groups for overlap),
        # not a reducibility verdict about being safely alternation-checkable
        # -- allow_unbounded=True consumes the unbounded quantifier instead
        # of bailing.
        assert _branch_charset(branch, allow_unbounded=True) == (True, expected)

    def test_branch_charset_allow_unbounded_still_bails_on_nested_group(self) -> None:
        # allow_unbounded only changes how a bare unbounded QUANTIFIER is
        # handled -- a nested group in the body is unconditionally
        # unreducible either way (the `ch in "(|)"` bailout runs first).
        assert _branch_charset("(?:x)+", allow_unbounded=True) == (False, None)

    @pytest.mark.parametrize(
        ("branch", "flags", "expected"),
        [
            ("a", 0, frozenset("a")),
            ("a", re.IGNORECASE, frozenset("aA")),
            (
                "[a-z]",
                re.IGNORECASE,
                frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            ),
        ],
    )
    def test_branch_charset_ignorecase_folds(
        self, branch: str, flags: int, expected: frozenset[str]
    ) -> None:
        # Security-review finding on the #200 fix: without case-folding, a
        # charset-overlap comparison under `re.IGNORECASE` would judge
        # `[a-z]` and `[A-Z]` disjoint by source text alone despite matching
        # the same characters at runtime.
        assert _branch_charset(branch, flags=flags) == (True, expected)

    def test_case_fold_charset_decomposes_multi_char_case_mapping(self) -> None:
        # Security re-review finding: Turkish "İ" (U+0130) lower-cases to the
        # TWO-character string "i̇" (base "i" + combining dot above), unlike
        # ASCII a/A. `_case_fold_charset` must decompose that into its
        # individual characters (via `set.update` iterating the string) so
        # the plain "i" lands in the folded set -- an earlier version added
        # the whole multi-char string as one opaque element instead, which
        # never overlapped a literal "i" elsewhere, reopening the exact
        # bypass class this function exists to close.
        folded = _case_fold_charset(frozenset("İ"), re.IGNORECASE)
        assert folded is not None
        assert "i" in folded

    def test_case_fold_charset_noop_without_ignorecase(self) -> None:
        assert _case_fold_charset(frozenset("aA"), 0) == frozenset("aA")

    def test_case_fold_charset_passes_through_unknown(self) -> None:
        assert _case_fold_charset(None, re.IGNORECASE) is None

    @pytest.mark.parametrize("frag", ["", "-?", r"\s*", "(?:foo)?", "x*"])
    def test_nullable_separator_true(self, frag: str) -> None:
        assert _is_nullable_separator(frag) is True

    @pytest.mark.parametrize("frag", ["-", "x", r"\s", "ab"])
    def test_nullable_separator_false(self, frag: str) -> None:
        assert _is_nullable_separator(frag) is False

    def test_nullable_separator_uncompilable_fragment_biases_unsafe(self) -> None:
        r"""A fragment that isn't a valid standalone pattern (e.g. a lone
        backreference `\1`) can't be proven non-nullable, so the conservative
        bias treats it as a nullable/unsafe separator."""
        assert _is_nullable_separator(r"\1") is True


class TestHasRe2:
    def test_returns_bool(self) -> None:
        assert isinstance(has_re2(), bool)
