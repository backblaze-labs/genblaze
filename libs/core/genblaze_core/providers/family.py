"""ModelFamily — pattern-keyed param-shape rule.

A ``ModelFamily`` is the SDK's unit of authority over model behavior. It
claims: "any slug matching this pattern uses this spec_template." Slugs
themselves are never stored — only the pattern that recognizes them, an
optional liveness probe (for providers without a discovery endpoint), and
``example_slugs`` used for documentation and nearest-neighbor suggestions.

The split between *pattern* (low rot — SDXL's ``text_prompts`` shape doesn't
change when NVIDIA renames a slug) and *slug list* (high rot — owned by
upstream's product team) is the architectural premise of this module: the
SDK ships shapes, not slugs.

See ``docs/exec-plans/active/model-registry-decoupling.md`` for the full
design and red-team trail.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from genblaze_core.providers.pattern_safety import assert_safe
from genblaze_core.providers.spec import ModelSpec

if TYPE_CHECKING:
    import httpx


class LiveProbeResult(StrEnum):
    """Outcome of a single ``FamilyProbe`` invocation."""

    LIVE = "live"
    """Upstream confirmed the slug is callable."""

    DEAD = "dead"
    """Upstream confirmed the slug is missing (404 / explicit not-found)."""

    UNKNOWN = "unknown"
    """Probe could not determine liveness — auth, network, or transport error."""


class FamilyProbe(Protocol):
    """Cheap upstream liveness check for a family-matched slug.

    Implementations must be cheap (one round-trip, no token spend) and
    polite (avoid creating persistent records in the upstream's audit log
    where possible). The canonical shapes:

    * **Catalog endpoint** — for providers without a full ``/v1/models``
      surface but with a single-model probe (HEAD on a model URL, etc.).
    * **Invalid-payload trick** — POST a deliberately-empty body. ``404``
      means the model is gone; ``400`` means it exists but the payload is
      invalid. Used by GMI, NVIDIA generative endpoints, etc.

    The probe receives an ``httpx.Client`` from the calling provider so
    timeout, retry, and auth are honored consistently.
    """

    def __call__(self, slug: str, *, http: httpx.Client) -> LiveProbeResult: ...


class DiscoverySupport(StrEnum):
    """Per-provider declaration of upstream catalog API support.

    Drives validation outcome semantics, probe-CI scope, and the user-facing
    capability surface. Every provider declares this as a class constant;
    the conformance test enforces presence.
    """

    NATIVE = "native"
    """Authoritative ``GET /models`` (or equivalent) covering the provider's
    full surface. NATIVE-matched slugs upgrade to ``OK_AUTHORITATIVE`` iff
    they appear in the live discovery cache."""

    PARTIAL = "partial"
    """Catalog exists but does not enumerate every endpoint (e.g., NVIDIA
    chat ``/v1/models`` does not list ``/genai/*`` generative endpoints).
    PARTIAL providers cannot return ``OK_AUTHORITATIVE`` from family-match
    alone — they need a ``FamilyProbe`` or fall back to ``OK_PROVISIONAL``
    with a strong WARN at preflight."""

    NONE = "none"
    """No catalog API. Family match returns ``OK_PROVISIONAL``. The user
    owns slug freshness; the SDK is honest that it cannot verify."""


@dataclass(frozen=True, slots=True)
class ModelFamily:
    """Pattern-keyed param-shape rule.

    Every slug matching ``pattern`` resolves to a copy of ``spec_template``
    with ``model_id`` substituted to the slug. Slugs are not stored on the
    family — that is the whole point.

    Args:
        name: Stable identifier for logs, metrics, error messages
            (``"nvidia-cosmos-video2world"``, ``"sdxl"``).
        pattern: Compiled regex, validated at construction for safety.
            Must be precompiled — the registry never re-compiles patterns
            at lookup time.
        spec_template: ``ModelSpec`` whose ``model_id`` is overridden per
            match. Carries param contracts, transformers, schemas,
            allowlist, input mapping, ``extras``. Pricing is intentionally
            omitted from the SDK going forward — users register pricing
            via ``registry.register_pricing(slug, strategy)``.
        description: One-line human-readable description for docs and
            error messages.
        example_slugs: Editorial slugs that match this family. Used for
            documentation, nearest-neighbor suggestions on ``NOT_FOUND``,
            and (for NATIVE providers) liveness gating in CI.
        unstable_examples: Slugs known or suspected dead — preserved
            through migration as a hint to maintainers and users until a
            ``probe`` is implemented and CI-passing for the relevant
            family. Replaces the legacy ``extras["suspected_dead"]``
            convention with a typed contract.
        probe: Optional ``FamilyProbe`` used by PARTIAL providers to
            confirm liveness. PARTIAL providers without a probe can only
            return ``OK_PROVISIONAL`` for family-matched slugs.
        discovery_required: If ``True``, the permissive fallback alone is
            insufficient — preflight must consult discovery (or fail).
            Reserved for families whose users universally expect strict
            preflight semantics.

    Pattern style guide:

    * Anchor with ``^`` and ``$`` whenever the family describes a closed
      set. ``re.compile(r"^stabilityai/stable-diffusion-xl(-base)?$")``
      beats ``re.compile(r"stabilityai/stable-diffusion-xl")``.
    * Prefer non-capturing groups ``(?:...)`` to capturing groups ``(...)``.
    * Avoid nested unbounded quantifiers — ``pattern_safety`` rejects them
      at construction.
    """

    name: str
    pattern: re.Pattern[str]
    spec_template: ModelSpec
    description: str
    example_slugs: tuple[str, ...] = ()
    # ``frozenset`` rather than ``tuple`` for O(1) membership checks —
    # the registry's ``validate()`` does ``slug in family.unstable_examples``
    # on every preflight, and a list of N unstable slugs would force a
    # linear scan. Callers may pass any iterable; ``__post_init__`` coerces.
    unstable_examples: frozenset[str] = field(default_factory=frozenset)
    probe: FamilyProbe | None = None
    discovery_required: bool = False

    def __post_init__(self) -> None:
        assert_safe(self.pattern)
        # Coerce ``unstable_examples`` to a frozenset if a tuple/list was
        # passed. ``object.__setattr__`` is the documented escape hatch
        # for frozen dataclass post-init normalization.
        if not isinstance(self.unstable_examples, frozenset):
            object.__setattr__(self, "unstable_examples", frozenset(self.unstable_examples))
        if self.spec_template.pricing is not None:
            # Pricing-by-family is the rot vector this plan eliminates;
            # users register pricing per-slug at runtime instead. Catch
            # the mistake at construction so connector PRs can't slip it
            # past review.
            raise ValueError(
                f"ModelFamily {self.name!r}: spec_template.pricing must be None "
                f"(pricing is user-registered via registry.register_pricing(...) "
                f"going forward — see docs/reference/pricing-recipes.md)."
            )

    def matches(self, model_id: str) -> bool:
        """True iff ``model_id`` matches this family's pattern.

        ``re.match`` anchors at the start of the string; ``fullmatch``
        adds an end anchor. For prefix-style family patterns
        (``^reve-edit``, no ``$``) we want ``match`` semantics; for
        closed-set patterns (``^bria-(?:genfill|eraser)$``) ``match``
        and ``fullmatch`` are equivalent because the pattern itself
        carries the ``$``. Using ``match`` exclusively is sufficient and
        avoids the redundant double-evaluation the previous
        ``fullmatch or match`` form did on every lookup.
        """
        return self.pattern.match(model_id) is not None

    def resolve(self, model_id: str) -> ModelSpec:
        """Return a ``ModelSpec`` for ``model_id`` derived from this family.

        The returned spec is the family's ``spec_template`` with
        ``model_id`` substituted. ``extras`` is shallow-copied so a
        consumer mutating ``spec.extras`` cannot corrupt the family's
        ``spec_template`` (which would silently affect every subsequent
        ``resolve()`` call from the same family).
        """
        from dataclasses import replace

        # Defensive copy of extras: ModelSpec is frozen, but its extras
        # field is a Mapping that's typically a plain dict at runtime —
        # a caller doing ``spec.extras["k"] = v`` mutates the shared
        # template otherwise.
        return replace(
            self.spec_template,
            model_id=model_id,
            extras=dict(self.spec_template.extras),
        )


@dataclass(frozen=True, slots=True)
class FamilyMatch:
    """Result of resolving a slug against a registry's families.

    ``spec`` is the family's ``spec_template`` with ``model_id`` substituted.
    Returned from ``ModelRegistry.match_family()``.
    """

    family: ModelFamily
    spec: ModelSpec


# Hard cap on the number of families per provider's registry. Keeps the
# linear-scan resolution under the perf budget regardless of how many
# connectors evolve. Connectors hitting the cap should consolidate
# patterns or split into multiple modality registries — not raise the
# cap.
MAX_PROVIDER_FAMILIES: int = 32


__all__ = [
    "DiscoverySupport",
    "FamilyMatch",
    "FamilyProbe",
    "LiveProbeResult",
    "MAX_PROVIDER_FAMILIES",
    "ModelFamily",
]
