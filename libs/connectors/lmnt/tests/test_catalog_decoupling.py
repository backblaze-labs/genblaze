"""Catalog-decoupling regression tests for ``genblaze-lmnt``.

LMNT declares ``DiscoverySupport.NONE`` — there's no upstream
``GET /models`` endpoint, no per-slug probe that doesn't enqueue
work, and the catalog is small + stable. The connector ships an
empty registry with a permissive fallback spec; every slug resolves
to the same baseline.

This file pins the contract for that minimal surface so a future
edit can't silently regress the post-decoupling shape.
"""

from __future__ import annotations

from genblaze_core.providers import (
    DiscoverySupport,
    ValidationOutcome,
)

# Note: these tests construct ``LMNTProvider`` directly without generating
# speech, so they don't need to touch the ``lmnt`` SDK at all — ``lmnt`` is
# a real installed dependency of this package (see test_lmnt_provider.py's
# ``test_make_client_matches_installed_sdk`` for the SDK-surface check).


# --- DiscoverySupport declaration -----------------------------------------


class TestDiscoverySupportDeclaration:
    def test_none(self) -> None:
        from genblaze_lmnt import LMNTProvider

        assert LMNTProvider.discovery_support is DiscoverySupport.NONE


# --- Registry shape (no families, fallback only) --------------------------


class TestRegistryShape:
    def test_no_provider_families_shipped(self) -> None:
        """LMNT ships zero provider-keyed families. Slugs resolve via
        the permissive fallback spec only."""
        from genblaze_lmnt import LMNTProvider

        provider = LMNTProvider(api_key="test")
        assert provider._models.families == ()

    def test_arbitrary_slug_resolves_via_fallback(self) -> None:
        """Any slug — current, future, typo — resolves through the
        fallback. The user owns slug freshness; the SDK does not
        gate."""
        from genblaze_lmnt import LMNTProvider

        provider = LMNTProvider(api_key="test")
        for slug in ("blizzard", "aurora", "future-lmnt-v3", "typo-slug"):
            spec = provider._models.get(slug)
            assert spec is not None, slug

    def test_validate_returns_unknown_permissive(self) -> None:
        """``validate_model`` for a NONE provider with no family match
        returns ``UNKNOWN_PERMISSIVE`` — slug isn't authoritatively
        confirmed, but the fallback lets it through."""
        from genblaze_lmnt import LMNTProvider

        provider = LMNTProvider(api_key="test")
        result = provider.validate_model("any-lmnt-slug")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


# --- Pricing-removed contract --------------------------------------------


class TestPricingPhaseOut:
    def test_fallback_spec_carries_no_pricing(self) -> None:
        """The connector's fallback spec ships with ``pricing=None``;
        cost is user-registered via ``register_pricing``."""
        from genblaze_lmnt import LMNTProvider

        provider = LMNTProvider(api_key="test")
        # Any slug that hits the fallback returns a spec with pricing=None.
        spec = provider._models.get("any-slug")
        assert spec.pricing is None


# --- Cross-provider isolation --------------------------------------------


class TestCrossProviderIsolation:
    def test_lmnt_does_not_match_other_provider_slugs(self) -> None:
        """LMNT has no families — ``match_family`` returns None for
        any slug; nothing matches, including slugs that look like
        another provider's catalog."""
        from genblaze_lmnt import LMNTProvider

        provider = LMNTProvider(api_key="test")
        for slug in (
            "veo-3.0-generate-001",
            "imagen-3.0-generate-002",
            "tts-1",
            "eleven_v2",
            "ray-2",
        ):
            assert provider._models.match_family(slug) is None, slug
