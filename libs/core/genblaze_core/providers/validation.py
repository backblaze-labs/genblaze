"""ValidationResult — the typed answer to "is this slug usable?".

``BaseProvider.validate_model(slug)`` returns a ``ValidationResult``. The
outcome is graded by what the SDK can honestly substantiate:

* ``OK_AUTHORITATIVE`` — positive confirmation from a user registration,
  a NATIVE discovery cache hit, or a ``FamilyProbe`` that returned LIVE.
* ``OK_PROVISIONAL`` — slug matched a family pattern but liveness is
  unverifiable on this provider's ``DiscoverySupport``. Pipeline preflight
  emits a WARN and proceeds; failures will surface mid-pipeline.
* ``UNKNOWN_PERMISSIVE`` — no family match. The permissive fallback
  applies; the slug passes through to upstream untouched. Preflight
  cannot say anything about liveness.
* ``NOT_FOUND`` — discovery says absent (NATIVE) or probe returned DEAD.
  Pipeline preflight raises before any wire calls.

The ``ValidationOutcome`` × ``ValidationSource`` matrix is the single
source of truth for slug-validity questions. ``probe_model()`` (deprecated
in 0.3.0, removed in 0.4.0) is a thin adapter over this surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ValidationSource(StrEnum):
    """Which layer of the registry produced the validation result."""

    USER = "user"
    """User-registered exact-match spec via ``registry.register(spec)``."""

    FAMILY = "family"
    """Family-pattern match (no probe consulted yet, no discovery hit)."""

    DISCOVERY = "discovery"
    """Slug found in the provider's discovery cache (NATIVE provider)."""

    PROBE = "probe"
    """``FamilyProbe`` was consulted and returned LIVE or DEAD."""

    FALLBACK = "fallback"
    """Permissive fallback — no family or discovery layer matched."""


class ValidationOutcome(StrEnum):
    """The graded answer to "is this slug usable?".

    Outcome ranking (strongest → weakest confirmation):
    OK_AUTHORITATIVE > OK_PROVISIONAL > UNKNOWN_PERMISSIVE; NOT_FOUND is
    a terminal-negative state distinct from the unknowns.
    """

    OK_AUTHORITATIVE = "ok_authoritative"
    """Positive confirmation: user-registered, NATIVE-discovery-confirmed,
    or family.probe() returned LIVE."""

    OK_PROVISIONAL = "ok_provisional"
    """Family-matched but liveness is unverifiable. Honest answer when the
    SDK matched a pattern but cannot confirm the slug is live (PARTIAL or
    NONE provider with no probe configured, or probe returned UNKNOWN)."""

    UNKNOWN_PERMISSIVE = "unknown_permissive"
    """No family match. Permissive fallback applies; the slug will pass
    through to upstream untouched. The SDK cannot pre-flight liveness."""

    NOT_FOUND = "not_found"
    """NATIVE discovery says the slug is absent, or family.probe() returned
    DEAD. Pipeline preflight raises ProviderError(MODEL_ERROR) for this
    outcome before any wire calls."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Structured validation outcome plus optional context.

    ``family_name`` is set when ``source`` is ``FAMILY``, ``DISCOVERY`` (if
    the discovered slug also matched a family), or ``PROBE``. ``detail``
    carries human-readable context for logs and error messages —
    ``"known_unstable; verify with discover_models()"`` for unstable
    examples, the upstream error text for ``NOT_FOUND``, etc.
    ``suggested_slugs`` populates the "Did you mean…?" hint on
    ``NOT_FOUND`` results — populated from family ``example_slugs`` and/or
    discovery-cache nearest-neighbor lookup when available.
    """

    outcome: ValidationOutcome
    source: ValidationSource
    family_name: str | None = None
    detail: str | None = None
    suggested_slugs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_ok(self) -> bool:
        """True for any positive outcome (authoritative or provisional).

        Convenience for callers that don't need to distinguish the
        confidence grade — e.g., the ``ModelRegistry.__contains__``
        membership check.
        """
        return self.outcome in (
            ValidationOutcome.OK_AUTHORITATIVE,
            ValidationOutcome.OK_PROVISIONAL,
        )

    @property
    def is_terminal_failure(self) -> bool:
        """True only for ``NOT_FOUND`` — the one outcome preflight raises on."""
        return self.outcome is ValidationOutcome.NOT_FOUND

    @classmethod
    def ok_authoritative(
        cls,
        source: ValidationSource,
        *,
        family_name: str | None = None,
        detail: str | None = None,
    ) -> ValidationResult:
        return cls(
            outcome=ValidationOutcome.OK_AUTHORITATIVE,
            source=source,
            family_name=family_name,
            detail=detail,
        )

    @classmethod
    def ok_provisional(
        cls,
        *,
        family_name: str,
        detail: str | None = None,
    ) -> ValidationResult:
        return cls(
            outcome=ValidationOutcome.OK_PROVISIONAL,
            source=ValidationSource.FAMILY,
            family_name=family_name,
            detail=detail,
        )

    @classmethod
    def unknown_permissive(cls, *, detail: str | None = None) -> ValidationResult:
        return cls(
            outcome=ValidationOutcome.UNKNOWN_PERMISSIVE,
            source=ValidationSource.FALLBACK,
            detail=detail,
        )

    @classmethod
    def not_found(
        cls,
        source: ValidationSource,
        *,
        family_name: str | None = None,
        detail: str | None = None,
        suggested_slugs: tuple[str, ...] = (),
    ) -> ValidationResult:
        return cls(
            outcome=ValidationOutcome.NOT_FOUND,
            source=source,
            family_name=family_name,
            detail=detail,
            suggested_slugs=suggested_slugs,
        )


__all__ = [
    "ValidationOutcome",
    "ValidationResult",
    "ValidationSource",
]
