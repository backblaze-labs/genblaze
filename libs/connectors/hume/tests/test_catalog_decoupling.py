"""Catalog-decoupling regression tests for ``genblaze-hume``.

Hume declares ``DiscoverySupport.NONE`` — there's no upstream
``GET /models`` endpoint (the Octave model is chosen via the request
``version`` field). The connector ships a single ``octave-*`` family plus
a permissive fallback; pricing is user-registered.

This file pins the post-decoupling shape so a future edit can't silently
regress it.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.providers import DiscoverySupport, ValidationOutcome


@pytest.fixture(autouse=True)
def _patch_hume_sdk():
    """Avoid importing the real hume SDK in tests."""
    with patch.dict(sys.modules, {"hume": MagicMock(), "hume.tts": MagicMock()}):
        yield


class TestDiscoverySupportDeclaration:
    def test_none(self) -> None:
        from genblaze_hume import HumeTTSProvider

        assert HumeTTSProvider.discovery_support is DiscoverySupport.NONE


class TestRegistryShape:
    def test_single_octave_family(self) -> None:
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key="test")
        names = {f.name for f in provider._models.families}
        assert names == {"hume-octave"}

    def test_octave_slugs_match_family(self) -> None:
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key="test")
        for slug in ("octave-1", "octave-2", "octave-3-preview"):
            assert provider._models.match_family(slug) is not None, slug

    def test_octave_validate_provisional(self) -> None:
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key="test")
        assert provider.validate_model("octave-2").outcome is ValidationOutcome.OK_PROVISIONAL

    def test_non_octave_resolves_via_fallback(self) -> None:
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key="test")
        spec = provider._models.get("not-an-octave-slug")
        assert spec is not None
        assert (
            provider.validate_model("not-an-octave-slug").outcome
            is ValidationOutcome.UNKNOWN_PERMISSIVE
        )


class TestPricingPhaseOut:
    def test_no_pricing_shipped(self) -> None:
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key="test")
        assert provider._models.get("octave-2").pricing is None
        assert provider._models.get("anything").pricing is None


class TestCrossProviderIsolation:
    def test_does_not_match_other_provider_slugs(self) -> None:
        from genblaze_hume import HumeTTSProvider

        provider = HumeTTSProvider(api_key="test")
        for slug in ("veo-3.0-generate-001", "tts-1", "eleven_v2", "ray-2", "lmnt-1"):
            assert provider._models.match_family(slug) is None, slug
