"""Tests for pattern_safety: catastrophic-backtracking guard."""

from __future__ import annotations

import re

import pytest
from genblaze_core.providers.pattern_safety import assert_safe, has_re2


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
        # The classic "(x+x+)+y" shape. Heuristic flags the nested
        # quantifier; that's enough to fail closed.
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            assert_safe(re.compile(r"(x+x+)+y"))


class TestHasRe2:
    def test_returns_bool(self) -> None:
        assert isinstance(has_re2(), bool)
