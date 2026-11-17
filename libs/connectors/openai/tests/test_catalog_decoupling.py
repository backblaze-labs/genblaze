"""Catalog-decoupling tests for genblaze-openai 0.3.0.

Coverage:
- ``DiscoverySupport.NATIVE`` declared on TTS, DALL-E, and Sora.
- Family-pattern resolution per modality, with cross-modality
  isolation (chat slugs don't match audio/image/video families).
- NATIVE discovery via ``client.models.list()`` filtered to
  family-matched slugs only — chat / embeddings / Whisper don't
  pollute the per-provider caches.
- ``validate_model`` outcomes for each provider.
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
def _patch_openai_sdk():
    """Avoid importing the real openai package in tests."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        yield


# --- DiscoverySupport declarations ----------------------------------------


class TestDiscoverySupportDeclarations:
    def test_tts_native(self) -> None:
        from genblaze_openai import OpenAITTSProvider

        assert OpenAITTSProvider.discovery_support is DiscoverySupport.NATIVE

    def test_dalle_native(self) -> None:
        from genblaze_openai import DalleProvider

        assert DalleProvider.discovery_support is DiscoverySupport.NATIVE

    def test_sora_native(self) -> None:
        from genblaze_openai import SoraProvider

        assert SoraProvider.discovery_support is DiscoverySupport.NATIVE


# --- Family resolution + cross-modality isolation -------------------------


class TestTTSFamily:
    def test_current_tts_models_match(self) -> None:
        from genblaze_openai import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test")
        for slug in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts"):
            match = provider._models.match_family(slug)
            assert match is not None and match.family.name == "openai-tts", slug

    def test_future_tts_variants_inherit(self) -> None:
        from genblaze_openai import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test")
        for slug in ("gpt-5-tts", "gpt-realtime-tts", "tts-2"):
            assert provider._models.match_family(slug) is not None, slug

    def test_chat_slug_doesnt_match(self) -> None:
        """A chat model passed to TTS provider should miss the family
        pattern — preflight will return UNKNOWN_PERMISSIVE rather than
        a misleading OK_AUTHORITATIVE from the cross-modality catalog."""
        from genblaze_openai import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test")
        for slug in ("gpt-4o", "gpt-4-turbo", "o1-mini"):
            assert provider._models.match_family(slug) is None, slug


class TestDalleFamilies:
    def test_gpt_image_family_matches(self) -> None:
        from genblaze_openai import DalleProvider

        provider = DalleProvider(api_key="test")
        for slug in ("gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini", "gpt-image-2"):
            match = provider._models.match_family(slug)
            assert match is not None and match.family.name == "openai-gpt-image", slug

    def test_dalle_legacy_family_matches(self) -> None:
        from genblaze_openai import DalleProvider

        provider = DalleProvider(api_key="test")
        for slug in ("dall-e-2", "dall-e-3"):
            match = provider._models.match_family(slug)
            assert match is not None and match.family.name == "openai-dalle", slug

    def test_chat_slug_doesnt_match(self) -> None:
        from genblaze_openai import DalleProvider

        provider = DalleProvider(api_key="test")
        for slug in ("gpt-4o", "gpt-4o-mini-tts"):
            assert provider._models.match_family(slug) is None, slug


class TestSoraFamily:
    def test_current_sora_models_match(self) -> None:
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test")
        for slug in ("sora-2", "sora-2-pro"):
            match = provider._models.match_family(slug)
            assert match is not None and match.family.name == "openai-sora", slug

    def test_future_sora_variants_inherit(self) -> None:
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test")
        for slug in ("sora-3", "sora-2-ultra"):
            assert provider._models.match_family(slug) is not None, slug

    def test_chat_slug_doesnt_match(self) -> None:
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test")
        for slug in ("gpt-4o", "tts-1", "dall-e-3"):
            assert provider._models.match_family(slug) is None, slug


# --- NATIVE discovery -----------------------------------------------------


def _mock_client_with_models(model_ids: list[str]) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.data = [SimpleNamespace(id=mid) for mid in model_ids]
    client.models.list.return_value = response
    return client


class TestTTSDiscovery:
    def test_discover_filters_to_tts_only(self) -> None:
        """The fetcher must filter out chat / image / Sora slugs so the
        TTS provider's cache contains only TTS-shaped slugs."""
        from genblaze_openai import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test")
        provider._client = _mock_client_with_models(
            ["tts-1", "tts-1-hd", "gpt-4o", "dall-e-3", "sora-2", "gpt-4o-mini-tts"]
        )
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.OK
        # Only TTS slugs survive the filter.
        assert result.slugs == frozenset({"tts-1", "tts-1-hd", "gpt-4o-mini-tts"})

    def test_validate_authoritative_for_cataloged_slug(self) -> None:
        from genblaze_openai import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test")
        provider._client = _mock_client_with_models(["tts-1", "gpt-4o"])
        result = provider.validate_model("tts-1")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.DISCOVERY


class TestDalleDiscovery:
    def test_discover_filters_to_image_only(self) -> None:
        from genblaze_openai import DalleProvider

        provider = DalleProvider(api_key="test")
        provider._client = _mock_client_with_models(
            ["dall-e-2", "dall-e-3", "gpt-image-1", "gpt-4o", "tts-1", "sora-2"]
        )
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.OK
        assert result.slugs == frozenset({"dall-e-2", "dall-e-3", "gpt-image-1"})


class TestSoraDiscovery:
    def test_discover_filters_to_sora_only(self) -> None:
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test")
        provider._client = _mock_client_with_models(["sora-2", "sora-2-pro", "gpt-4o", "dall-e-3"])
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.OK
        assert result.slugs == frozenset({"sora-2", "sora-2-pro"})

    def test_base_url_keeps_compatible_server_catalog(self) -> None:
        """With ``base_url`` set, the server's own video slugs are not
        filtered out, so they validate instead of failing with NOT_FOUND."""
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test", base_url="http://localhost:8091/v1")
        provider._client = _mock_client_with_models(["wan-2.2-ti2v", "sora-2"])
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.OK
        assert result.slugs == frozenset({"wan-2.2-ti2v", "sora-2"})
        assert result.source_url == "http://localhost:8091/v1/models"
        ok = ValidationOutcome.OK_AUTHORITATIVE
        assert provider.validate_model("wan-2.2-ti2v").outcome is ok
        assert provider.validate_model("not-listed").outcome is ValidationOutcome.NOT_FOUND


# --- Pricing-removed contract --------------------------------------------


class TestPricingPhaseOut:
    def test_tts_default_spec_no_pricing(self) -> None:
        from genblaze_openai import OpenAITTSProvider

        provider = OpenAITTSProvider(api_key="test")
        assert provider._models.get("tts-1").pricing is None
        assert provider._models.get("gpt-4o-mini-tts").pricing is None

    def test_dalle_default_spec_no_pricing(self) -> None:
        from genblaze_openai import DalleProvider

        provider = DalleProvider(api_key="test")
        assert provider._models.get("dall-e-3").pricing is None
        assert provider._models.get("gpt-image-1").pricing is None

    def test_sora_default_spec_no_pricing(self) -> None:
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test")
        assert provider._models.get("sora-2").pricing is None
