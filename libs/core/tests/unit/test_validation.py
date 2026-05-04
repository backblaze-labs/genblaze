"""Tests for ValidationResult: outcome semantics and constructors."""

from __future__ import annotations

from genblaze_core.providers.validation import (
    ValidationOutcome,
    ValidationResult,
    ValidationSource,
)


class TestConstructors:
    def test_ok_authoritative(self) -> None:
        r = ValidationResult.ok_authoritative(ValidationSource.USER, family_name="sdxl")
        assert r.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert r.source is ValidationSource.USER
        assert r.family_name == "sdxl"
        assert r.detail is None
        assert r.suggested_slugs == ()

    def test_ok_provisional(self) -> None:
        r = ValidationResult.ok_provisional(family_name="nvidia-audio", detail="known_unstable")
        assert r.outcome is ValidationOutcome.OK_PROVISIONAL
        assert r.source is ValidationSource.FAMILY
        assert r.family_name == "nvidia-audio"
        assert r.detail == "known_unstable"

    def test_unknown_permissive(self) -> None:
        r = ValidationResult.unknown_permissive(detail="no family matched")
        assert r.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE
        assert r.source is ValidationSource.FALLBACK
        assert r.family_name is None

    def test_not_found_with_suggestions(self) -> None:
        r = ValidationResult.not_found(
            ValidationSource.DISCOVERY,
            detail="upstream catalog returned 200 but slug absent",
            suggested_slugs=("nvidia/magpie-tts-multilingual",),
        )
        assert r.outcome is ValidationOutcome.NOT_FOUND
        assert r.source is ValidationSource.DISCOVERY
        assert "nvidia/magpie-tts-multilingual" in r.suggested_slugs


class TestSemantics:
    def test_is_ok_for_authoritative(self) -> None:
        r = ValidationResult.ok_authoritative(ValidationSource.USER)
        assert r.is_ok is True
        assert r.is_terminal_failure is False

    def test_is_ok_for_provisional(self) -> None:
        r = ValidationResult.ok_provisional(family_name="x")
        assert r.is_ok is True
        assert r.is_terminal_failure is False

    def test_is_ok_false_for_unknown_permissive(self) -> None:
        # __contains__ should return False for both UNKNOWN_PERMISSIVE
        # and NOT_FOUND. is_ok is the canonical predicate.
        r = ValidationResult.unknown_permissive()
        assert r.is_ok is False
        assert r.is_terminal_failure is False

    def test_is_terminal_failure_only_for_not_found(self) -> None:
        r = ValidationResult.not_found(ValidationSource.DISCOVERY)
        assert r.is_ok is False
        assert r.is_terminal_failure is True


class TestEnums:
    def test_validation_outcome_values(self) -> None:
        assert ValidationOutcome.OK_AUTHORITATIVE.value == "ok_authoritative"
        assert ValidationOutcome.OK_PROVISIONAL.value == "ok_provisional"
        assert ValidationOutcome.UNKNOWN_PERMISSIVE.value == "unknown_permissive"
        assert ValidationOutcome.NOT_FOUND.value == "not_found"

    def test_validation_source_values(self) -> None:
        assert ValidationSource.USER.value == "user"
        assert ValidationSource.FAMILY.value == "family"
        assert ValidationSource.DISCOVERY.value == "discovery"
        assert ValidationSource.PROBE.value == "probe"
        assert ValidationSource.FALLBACK.value == "fallback"


class TestFrozen:
    def test_validation_result_is_immutable(self) -> None:
        r = ValidationResult.ok_authoritative(ValidationSource.USER)
        try:
            r.outcome = ValidationOutcome.NOT_FOUND  # type: ignore[misc]
        except (AttributeError, Exception):
            pass
        else:
            raise AssertionError("ValidationResult should be frozen")
