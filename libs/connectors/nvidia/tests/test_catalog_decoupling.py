"""Tests for the catalog-decoupled architecture in genblaze-nvidia 0.3.0.

Coverage:
- ``DiscoverySupport`` declarations on each NVIDIA provider.
- Family pattern resolution for audio / video / image (SDXL vs SD3 vs FLUX).
- ``empty_payload_genai_probe`` translation (404=DEAD, 400=LIVE, etc.).
- End-to-end ``validate_model()`` for PARTIAL providers via the probe.
- ``NvidiaChatProvider.discover_models()`` + ``validate_model()`` via
  the OpenAI-compatible /v1/models endpoint.
- The originating reporter bug: ``nvidia/riva-tts`` surfaces as
  ``NOT_FOUND`` at preflight (no longer a mid-pipeline 404).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from genblaze_core.providers import (
    DiscoveryStatus,
    DiscoverySupport,
    LiveProbeResult,
    ValidationOutcome,
    ValidationSource,
)
from genblaze_nvidia import (
    NvidiaAudioProvider,
    NvidiaChatProvider,
    NvidiaImageProvider,
    NvidiaVideoProvider,
)
from genblaze_nvidia._probe import empty_payload_genai_probe


# --- DiscoverySupport declarations -----------------------------------------


class TestDiscoverySupportDeclarations:
    def test_chat_native(self) -> None:
        assert NvidiaChatProvider.discovery_support is DiscoverySupport.NATIVE

    def test_audio_partial(self) -> None:
        assert NvidiaAudioProvider.discovery_support is DiscoverySupport.PARTIAL

    def test_image_partial(self) -> None:
        assert NvidiaImageProvider.discovery_support is DiscoverySupport.PARTIAL

    def test_video_partial(self) -> None:
        assert NvidiaVideoProvider.discovery_support is DiscoverySupport.PARTIAL


# --- Family resolution -----------------------------------------------------


class TestAudioFamilyResolution:
    def test_fugatto_routes_to_music_family(self) -> None:
        provider = NvidiaAudioProvider(api_key="nvapi-test")
        match = provider._models.match_family("nvidia/fugatto")
        assert match is not None
        assert match.family.name == "nvidia-audio-music"
        assert match.spec.extras.get("is_music") is True

    def test_magpie_tts_routes_to_voice_family(self) -> None:
        provider = NvidiaAudioProvider(api_key="nvapi-test")
        match = provider._models.match_family("nvidia/magpie-tts-multilingual")
        assert match is not None
        assert match.family.name == "nvidia-audio-voice"
        assert match.spec.extras.get("is_music") is False

    def test_riva_tts_still_pattern_matches_voice_family(self) -> None:
        """The retired ``nvidia/riva-tts`` slug still pattern-matches the
        voice family — but the probe will surface DEAD at preflight."""
        provider = NvidiaAudioProvider(api_key="nvapi-test")
        match = provider._models.match_family("nvidia/riva-tts")
        assert match is not None
        assert match.family.name == "nvidia-audio-voice"

    def test_unrelated_slug_no_match(self) -> None:
        provider = NvidiaAudioProvider(api_key="nvapi-test")
        match = provider._models.match_family("openai/whisper")
        assert match is None


class TestVideoFamilyResolution:
    def test_text2world_routes_to_text2world_family(self) -> None:
        provider = NvidiaVideoProvider(api_key="nvapi-test")
        match = provider._models.match_family("nvidia/cosmos-2.0-diffusion-text2world")
        assert match is not None
        assert match.family.name == "nvidia-cosmos-text2world"

    def test_video2world_routes_to_video2world_family(self) -> None:
        provider = NvidiaVideoProvider(api_key="nvapi-test")
        match = provider._models.match_family("nvidia/cosmos-1.0-7b-diffusion-video2world")
        assert match is not None
        assert match.family.name == "nvidia-cosmos-video2world"


class TestImageFamilyResolution:
    def test_sdxl_wins_over_sd3(self) -> None:
        """SDXL pattern is checked first because both share the
        ``stabilityai/`` namespace but the payload shape differs."""
        provider = NvidiaImageProvider(api_key="nvapi-test")
        match = provider._models.match_family("stabilityai/stable-diffusion-xl")
        assert match is not None
        assert match.family.name == "nvidia-image-sdxl"
        # Confirm the SDXL transformer is wired through.
        assert match.spec.param_transformer is not None

    def test_sd3_routes_correctly(self) -> None:
        provider = NvidiaImageProvider(api_key="nvapi-test")
        match = provider._models.match_family("stabilityai/stable-diffusion-3-5-large")
        assert match is not None
        assert match.family.name == "nvidia-image-sd3"
        # SD3 has no payload transformer (modern ``prompt`` field).
        assert match.spec.param_transformer is None

    def test_flux_routes_correctly(self) -> None:
        provider = NvidiaImageProvider(api_key="nvapi-test")
        match = provider._models.match_family("black-forest-labs/flux.1-dev")
        assert match is not None
        assert match.family.name == "nvidia-image-flux"


# --- empty_payload_genai_probe primitive -----------------------------------


def _http_with_status(status: int) -> MagicMock:
    """Mock httpx.Client that returns ``status`` on POST."""
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    http.post.return_value = resp
    return http


class TestEmptyPayloadProbe:
    def test_404_means_dead(self) -> None:
        result = empty_payload_genai_probe("nvidia/dead-slug", http=_http_with_status(404))
        assert result is LiveProbeResult.DEAD

    def test_400_means_live(self) -> None:
        """400 = the slug exists; we just sent a malformed payload (which
        is exactly what the empty-payload trick is designed to elicit)."""
        result = empty_payload_genai_probe("nvidia/live-slug", http=_http_with_status(400))
        assert result is LiveProbeResult.LIVE

    def test_2xx_means_live(self) -> None:
        result = empty_payload_genai_probe("nvidia/forgiving-slug", http=_http_with_status(200))
        assert result is LiveProbeResult.LIVE

    def test_401_403_inconclusive(self) -> None:
        """Auth failures don't tell us whether the slug exists — only that
        we're not allowed to ask."""
        for status in (401, 403):
            result = empty_payload_genai_probe(
                "nvidia/cosmos-1.0-7b-diffusion-text2world",
                http=_http_with_status(status),
            )
            assert result is LiveProbeResult.UNKNOWN, f"status={status}"

    def test_5xx_inconclusive(self) -> None:
        result = empty_payload_genai_probe("nvidia/anything", http=_http_with_status(503))
        assert result is LiveProbeResult.UNKNOWN

    def test_transport_error_inconclusive(self) -> None:
        http = MagicMock()
        http.post.side_effect = RuntimeError("connection refused")
        result = empty_payload_genai_probe("nvidia/anything", http=http)
        assert result is LiveProbeResult.UNKNOWN


# --- validate_model end-to-end on PARTIAL providers ------------------------


def _provider_with_probe_status(cls: type, status: int) -> object:
    """Return a NVIDIA generative provider whose internal http client
    yields ``status`` on POST — i.e., the empty-payload probe sees that
    response."""
    provider = cls(api_key="nvapi-test", http_client=_http_with_status(status))
    return provider


class TestValidateModelPartialProviders:
    def test_audio_dead_slug_surfaces_not_found(self) -> None:
        """The originating reporter bug: ``nvidia/riva-tts`` is dead
        upstream. Probe returns DEAD → validate_model returns NOT_FOUND
        → Pipeline preflight raises before any wire calls."""
        provider = _provider_with_probe_status(NvidiaAudioProvider, status=404)
        result = provider.validate_model("nvidia/riva-tts")
        assert result.outcome is ValidationOutcome.NOT_FOUND
        assert result.source is ValidationSource.PROBE
        assert "DEAD" in (result.detail or "")

    def test_audio_live_slug_authoritative(self) -> None:
        """Magpie-TTS exists upstream — probe returns LIVE (via a 400
        rejection of the empty payload) → validate_model returns
        OK_AUTHORITATIVE."""
        provider = _provider_with_probe_status(NvidiaAudioProvider, status=400)
        result = provider.validate_model("nvidia/magpie-tts-multilingual")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.PROBE

    def test_image_sdxl_live_slug_authoritative(self) -> None:
        provider = _provider_with_probe_status(NvidiaImageProvider, status=400)
        result = provider.validate_model("stabilityai/stable-diffusion-xl")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE

    def test_video_cosmos_live_slug_authoritative(self) -> None:
        provider = _provider_with_probe_status(NvidiaVideoProvider, status=400)
        result = provider.validate_model("nvidia/cosmos-2.0-diffusion-text2world")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE

    def test_unknown_namespace_falls_through_permissive(self) -> None:
        """A slug that doesn't match any NVIDIA family hits the permissive
        fallback. validate_model returns UNKNOWN_PERMISSIVE; preflight
        emits a one-time WARN and proceeds."""
        provider = _provider_with_probe_status(NvidiaAudioProvider, status=404)
        result = provider.validate_model("openai/whisper")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


# --- NvidiaChatProvider NATIVE discovery -----------------------------------


class TestChatNativeDiscovery:
    def _make_chat_with_models(
        self, slug_ids: list[str], should_raise: bool = False
    ) -> NvidiaChatProvider:
        """Build a chat provider whose injected client returns ``slug_ids``
        from ``models.list()``."""
        client = MagicMock()
        if should_raise:
            client.models.list.side_effect = RuntimeError("auth failed")
        else:
            page = MagicMock()
            page.data = [MagicMock(id=s) for s in slug_ids]
            client.models.list.return_value = page
        provider = NvidiaChatProvider(api_key="nvapi-test", client=client)
        return provider

    def test_discover_models_returns_live_catalog(self) -> None:
        provider = self._make_chat_with_models(
            ["nvidia/nemotron-3-nano-omni", "meta/llama-3.3-70b-instruct"]
        )
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.OK
        assert "nvidia/nemotron-3-nano-omni" in result.slugs
        assert "meta/llama-3.3-70b-instruct" in result.slugs

    def test_discover_models_failure_returns_failed(self) -> None:
        provider = self._make_chat_with_models([], should_raise=True)
        result = provider.discover_models()
        assert result.status is DiscoveryStatus.FAILED
        assert "auth failed" in (result.detail or "")

    def test_validate_model_authoritative_for_cataloged_slug(self) -> None:
        provider = self._make_chat_with_models(
            ["nvidia/nemotron-3-nano-omni", "meta/llama-3.3-70b-instruct"]
        )
        result = provider.validate_model("meta/llama-3.3-70b-instruct")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.DISCOVERY

    def test_validate_model_not_found_for_missing_slug(self) -> None:
        provider = self._make_chat_with_models(["nvidia/nemotron-3-nano-omni"])
        result = provider.validate_model("nvidia/nonexistent-slug")
        assert result.outcome is ValidationOutcome.NOT_FOUND
        assert result.source is ValidationSource.DISCOVERY

    def test_discovery_cache_single_flight(self) -> None:
        """Successive validate_model calls don't re-fetch the catalog."""
        provider = self._make_chat_with_models(["nvidia/nemotron-3-nano-omni"])
        provider.validate_model("nvidia/nemotron-3-nano-omni")
        provider.validate_model("nvidia/nonexistent-slug")
        provider.validate_model("nvidia/another-missing")
        # First call triggers the fetch; subsequent calls hit the cache.
        assert provider._injected_client.models.list.call_count == 1


# --- The reporter bug, end-to-end ------------------------------------------


def test_riva_tts_surfaces_at_preflight_not_mid_pipeline() -> None:
    """End-to-end: F-2026-05-04-01 reproduces. Before catalog decoupling,
    ``nvidia/riva-tts`` would silently pass the registry's permissive
    fallback and 404 in the middle of a pipeline. Now it surfaces as
    NOT_FOUND at preflight via the empty-payload probe.
    """
    from genblaze_core import Pipeline
    from genblaze_core.exceptions import ProviderError
    from genblaze_core.models.enums import Modality

    provider = _provider_with_probe_status(NvidiaAudioProvider, status=404)
    pipe = Pipeline("repro").step(
        provider,
        model="nvidia/riva-tts",
        modality=Modality.AUDIO,
        prompt="hello",
    )
    with pytest.raises(ProviderError) as exc:
        pipe._validate_steps()
    msg = str(exc.value).lower()
    assert "not found" in msg
    assert "nvidia/riva-tts" in msg
