"""Tests for pattern_safety: catastrophic-backtracking guard."""

from __future__ import annotations

import re

import pytest
from genblaze_core.providers.pattern_safety import _heuristic_unsafe, assert_safe, has_re2


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

    @pytest.mark.skipif(has_re2(), reason="re2 enforces its own checks")
    def test_nested_unbounded_plus_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a+)+"))

    @pytest.mark.skipif(has_re2(), reason="re2 enforces its own checks")
    def test_nested_unbounded_star_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(.+)+"))

    @pytest.mark.skipif(has_re2(), reason="re2 enforces its own checks")
    def test_duplicate_alternation_rejected(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(a|a)*"))

    @pytest.mark.skipif(has_re2(), reason="re2 enforces its own checks")
    def test_realistic_evil_pattern_rejected(self) -> None:
        # The classic "(x+x+)+y" shape. Heuristic flags the nested quantifier;
        # that's enough to fail closed. Assembled at runtime (this fixture is only
        # analyzed by assert_safe, never matched) so it isn't itself flagged as a
        # static ReDoS literal by code scanning.
        evil = f"({'x+' * 2})+y"  # -> (x+x+)+y
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(evil))

    @pytest.mark.skipif(not has_re2(), reason="requires re2 to exercise the authoritative branch")
    def test_re2_rejects_backreference(self) -> None:
        # Backreferences aren't supported by RE2's linear-time engine — this
        # exercises the authoritative branch itself (distinct from the
        # heuristic-shape tests above, which are skipped once re2 is active).
        with pytest.raises(ValueError, match="rejected by google-re2"):
            assert_safe(re.compile(r"(a)\1"))


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
        ],
    )
    def test_passes_known_good_shapes(self, src: str) -> None:
        assert _heuristic_unsafe(src) is False


class TestHasRe2:
    def test_returns_bool(self) -> None:
        assert isinstance(has_re2(), bool)
