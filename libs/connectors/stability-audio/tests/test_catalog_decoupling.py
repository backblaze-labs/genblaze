"""Catalog-decoupling tests for genblaze-stability-audio 0.3.0.

Coverage:

* ``DiscoverySupport.NONE`` declared on StabilityAudioProvider.
* Family-pattern resolution: ``stable-audio-2.5`` matches, future
  ``stable-audio-N`` variants inherit, non-family slugs fall through
  to the permissive fallback.
* Pricing-removed contract: registry default specs carry no pricing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.providers import DiscoverySupport


@pytest.fixture(autouse=True)
def _patch_httpx():
    """Avoid importing the real httpx package in tests."""
    with patch.dict("sys.modules", {"httpx": MagicMock()}):
        yield


# --- DiscoverySupport declaration -----------------------------------------


class TestDiscoverySupportDeclaration:
    def test_none(self) -> None:
        from genblaze_stability_audio import StabilityAudioProvider

        assert StabilityAudioProvider.discovery_support is DiscoverySupport.NONE


# --- Family resolution ----------------------------------------------------


class TestStableAudioFamily:
    def test_current_model_matches(self) -> None:
        from genblaze_stability_audio import StabilityAudioProvider

        provider = StabilityAudioProvider(api_key="test")
        match = provider._models.match_family("stable-audio-2.5")
        assert match is not None and match.family.name == "stability-stable-audio"

    def test_future_variants_inherit(self) -> None:
        from genblaze_stability_audio import StabilityAudioProvider

        provider = StabilityAudioProvider(api_key="test")
        for slug in ("stable-audio-3", "stable-audio-3.0", "stable-audio-pro"):
            assert provider._models.match_family(slug) is not None, slug

    def test_unrelated_slugs_dont_match(self) -> None:
        from genblaze_stability_audio import StabilityAudioProvider

        provider = StabilityAudioProvider(api_key="test")
        for slug in ("ray-2", "tts-1", "veo-3.0-generate-001", "sora-2"):
            assert provider._models.match_family(slug) is None, slug


# --- Pricing-removed contract --------------------------------------------


class TestPricingPhaseOut:
    def test_default_spec_no_pricing(self) -> None:
        from genblaze_stability_audio import StabilityAudioProvider

        provider = StabilityAudioProvider(api_key="test")
        for slug in ("stable-audio-2.5", "stable-audio-3"):
            assert provider._models.get(slug).pricing is None, slug
