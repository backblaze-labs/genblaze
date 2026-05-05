"""Catalog-decoupling tests for genblaze-elevenlabs 0.3.0.

Coverage:
- ``DiscoverySupport.NATIVE`` on TTS, ``NONE`` on SFX.
- Family pattern resolution covers current + future eleven_* slugs.
- TTS discovery via ``client.models.get_all()`` populates the cache.
- ``validate_model`` outcomes for both providers.
- Pricing-removed contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.providers import (
    DiscoveryStatus,
    DiscoverySupport,
    ValidationOutcome,
    ValidationSource,
)


@pytest.fixture(autouse=True)
def _patch_elevenlabs_sdk():
    """Avoid importing the real elevenlabs package in tests."""
    with patch.dict(
        "sys.modules",
        {"elevenlabs": MagicMock(), "elevenlabs.client": MagicMock()},
    ):
        yield


# --- DiscoverySupport declarations ----------------------------------------


class TestDiscoverySupportDeclarations:
    def test_tts_native(self) -> None:
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        assert ElevenLabsTTSProvider.discovery_support is DiscoverySupport.NATIVE

    def test_sfx_none(self) -> None:
        from genblaze_elevenlabs import ElevenLabsSFXProvider

        assert ElevenLabsSFXProvider.discovery_support is DiscoverySupport.NONE


# --- Family resolution -----------------------------------------------------


class TestTTSFamily:
    def test_current_eleven_models_match(self) -> None:
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        provider = ElevenLabsTTSProvider(api_key="test")
        for slug in (
            "eleven_v3",
            "eleven_multilingual_v2",
            "eleven_flash_v2_5",
            "eleven_turbo_v2_5",
        ):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "elevenlabs-tts", slug

    def test_future_eleven_variants_inherit(self) -> None:
        """``^eleven_`` absorbs future variants without code changes."""
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        provider = ElevenLabsTTSProvider(api_key="test")
        for slug in ("eleven_v4", "eleven_pro_2026", "eleven_studio"):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "elevenlabs-tts", slug


class TestSFXFamily:
    def test_current_sfx_model_matches(self) -> None:
        from genblaze_elevenlabs import ElevenLabsSFXProvider

        provider = ElevenLabsSFXProvider(api_key="test")
        match = provider._models.match_family("eleven_text_to_sound_v2")
        assert match is not None
        assert match.family.name == "elevenlabs-sfx"

    def test_future_sfx_variants_inherit(self) -> None:
        from genblaze_elevenlabs import ElevenLabsSFXProvider

        provider = ElevenLabsSFXProvider(api_key="test")
        for slug in ("eleven_text_to_sound_v3", "eleven_text_to_sound_pro"):
            match = provider._models.match_family(slug)
            assert match is not None, slug


# --- TTS NATIVE discovery -------------------------------------------------


class TestTTSDiscovery:
    def _make_provider_with_models(self, model_ids: list[str]):
        """Build a TTS provider whose injected client returns ``model_ids``
        from ``models.get_all()``."""
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        client = MagicMock()
        client.models.get_all.return_value = [SimpleNamespace(model_id=mid) for mid in model_ids]
        provider = ElevenLabsTTSProvider(api_key="test")
        provider._client = client
        return provider, client

    def test_discover_models_returns_live_catalog(self) -> None:
        provider, _ = self._make_provider_with_models(["eleven_v3", "eleven_multilingual_v2"])
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.OK
        assert "eleven_v3" in result.slugs
        assert "eleven_multilingual_v2" in result.slugs

    def test_discover_models_failure_returns_failed(self) -> None:
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        provider = ElevenLabsTTSProvider(api_key="test")
        client = MagicMock()
        client.models.get_all.side_effect = RuntimeError("auth failed")
        provider._client = client

        result = provider.discover_models()
        assert result.status is DiscoveryStatus.FAILED

    def test_validate_model_authoritative_for_cataloged_slug(self) -> None:
        provider, _ = self._make_provider_with_models(["eleven_v3", "eleven_multilingual_v2"])
        result = provider.validate_model("eleven_v3")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.DISCOVERY

    def test_validate_model_not_found_for_missing_slug(self) -> None:
        provider, _ = self._make_provider_with_models(["eleven_v3"])
        result = provider.validate_model("eleven_retired_v0")
        assert result.outcome is ValidationOutcome.NOT_FOUND

    def test_discovery_cache_single_flight(self) -> None:
        """Successive validate_model calls don't re-fetch the catalog."""
        provider, client = self._make_provider_with_models(["eleven_v3"])
        provider.validate_model("eleven_v3")
        provider.validate_model("eleven_retired_v0")
        provider.validate_model("eleven_another")
        assert client.models.get_all.call_count == 1


# --- SFX validate_model (NONE — provisional only) ------------------------


class TestSFXValidateModel:
    def test_family_matched_provisional(self) -> None:
        from genblaze_elevenlabs import ElevenLabsSFXProvider

        provider = ElevenLabsSFXProvider(api_key="test")
        result = provider.validate_model("eleven_text_to_sound_v2")
        assert result.outcome is ValidationOutcome.OK_PROVISIONAL
        assert result.family_name == "elevenlabs-sfx"

    def test_unmatched_unknown_permissive(self) -> None:
        from genblaze_elevenlabs import ElevenLabsSFXProvider

        provider = ElevenLabsSFXProvider(api_key="test")
        result = provider.validate_model("not-a-sound-effect-slug")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


# --- Pricing-removed contract --------------------------------------------


class TestPricingPhaseOut:
    def test_tts_default_spec_no_pricing(self) -> None:
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        provider = ElevenLabsTTSProvider(api_key="test")
        assert provider._models.get("eleven_v3").pricing is None

    def test_sfx_default_spec_no_pricing(self) -> None:
        from genblaze_elevenlabs import ElevenLabsSFXProvider

        provider = ElevenLabsSFXProvider(api_key="test")
        assert provider._models.get("eleven_text_to_sound_v2").pricing is None
