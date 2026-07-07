"""Tests for the catalog-decoupled architecture in genblaze-gmicloud 0.3.0.

Coverage:
- ``DiscoverySupport.PARTIAL`` declared on every GMI provider.
- Family-pattern resolution per modality (audio TTS / clone / music,
  image bria-inpaint / edit, video pixverse / wan-r2v / base).
- ``unstable_examples`` propagates through ``validate_model()`` as
  ``OK_PROVISIONAL`` with a ``known_unstable`` detail (RT-10).
- ``empty_payload_request_probe`` translation (404=DEAD, 400=LIVE, etc.).
- End-to-end ``validate_model()`` outcomes via the probe.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from genblaze_core.providers import (
    DiscoverySupport,
    LiveProbeResult,
    ValidationOutcome,
    ValidationSource,
)
from genblaze_gmicloud import (
    GMICloudAudioProvider,
    GMICloudImageProvider,
    GMICloudVideoProvider,
)
from genblaze_gmicloud._probe import empty_payload_request_probe

# --- DiscoverySupport declarations -----------------------------------------


class TestDiscoverySupportDeclarations:
    def test_audio_partial(self) -> None:
        assert GMICloudAudioProvider.discovery_support is DiscoverySupport.PARTIAL

    def test_image_partial(self) -> None:
        assert GMICloudImageProvider.discovery_support is DiscoverySupport.PARTIAL

    def test_video_partial(self) -> None:
        assert GMICloudVideoProvider.discovery_support is DiscoverySupport.PARTIAL


# --- Family resolution -----------------------------------------------------


class TestAudioFamilyResolution:
    """Audio families in 0.3.2+ use case-insensitive patterns with
    ``canonical_slug=str.lower`` so pre-existing PascalCase callers
    continue to match the right family AND the wire form is rewritten to
    the lowercase canonical (per GMI's 2026-03-10 published catalog)."""

    def test_voice_clone_routes_to_clone_family(self) -> None:
        """GMI's 2026-03-10 catalog ships the slug as
        ``minimax-audio-voice-clone-speech-2.6-hd`` (note the ``-audio-``
        segment). The family's canonical_slug rewrites both legacy
        (``MiniMax-Voice-Clone-*``, no ``-Audio-``) and current
        (``minimax-audio-voice-clone-*``) callers to the published wire
        form. Test both inputs."""
        provider = GMICloudAudioProvider(api_key="test")
        # Legacy PascalCase without -Audio- segment.
        match = provider._models.match_family("MiniMax-Voice-Clone-Speech-2.6-HD")
        assert match is not None
        assert match.family.name == "gmi-audio-clone"
        assert match.spec.model_id == "minimax-audio-voice-clone-speech-2.6-hd"
        # Current canonical (already includes -audio- + lowercase).
        match2 = provider._models.match_family("minimax-audio-voice-clone-speech-2.6-hd")
        assert match2 is not None
        assert match2.family.name == "gmi-audio-clone"
        assert match2.spec.model_id == "minimax-audio-voice-clone-speech-2.6-hd"

    def test_music_routes_to_music_family(self) -> None:
        provider = GMICloudAudioProvider(api_key="test")
        match = provider._models.match_family("MiniMax-Music-2.5")
        assert match is not None
        assert match.family.name == "gmi-audio-music"
        assert match.spec.extras.get("is_music") is True
        assert match.spec.model_id == "minimax-music-2.5"

    def test_tts_routes_to_tts_family(self) -> None:
        provider = GMICloudAudioProvider(api_key="test")
        # Mix PascalCase + lowercase to prove both casings match.
        for slug, expected_wire in (
            ("ElevenLabs-TTS-v3", "elevenlabs-tts-v3"),
            ("MiniMax-TTS-Speech-2.6-Turbo", "minimax-tts-speech-2.6-turbo"),
            ("inworld-tts-1.5-mini", "inworld-tts-1.5-mini"),
        ):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "gmi-audio-tts", slug
            assert match.spec.model_id == expected_wire, slug


class TestImageFamilyResolution:
    def test_bria_inpaint_routes_correctly(self) -> None:
        provider = GMICloudImageProvider(api_key="test")
        for slug in ("bria-genfill", "bria-eraser"):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "gmi-image-bria-inpaint", slug
            # Spec must carry the inpaint allowlist (mask, mask_url, etc.).
            assert "mask" in (match.spec.param_allowlist or set())

    def test_edit_family_covers_seededit_and_reve(self) -> None:
        provider = GMICloudImageProvider(api_key="test")
        for slug in (
            "seededit-3-0-i2i-250628",
            "reve-edit-20250915",
            "reve-remix-20250915",
        ):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "gmi-image-edit", slug
            assert "image_url" in (match.spec.param_allowlist or set())

    def test_seedream_falls_through_to_fallback(self) -> None:
        """Seedream uses the base surface — no specialized family needed."""
        provider = GMICloudImageProvider(api_key="test")
        match = provider._models.match_family("seedream-5.0-lite")
        assert match is None  # falls through to permissive fallback


class TestVideoFamilyResolution:
    def test_pixverse_routes_to_pixverse_family(self) -> None:
        provider = GMICloudVideoProvider(api_key="test")
        for slug in (
            "pixverse-v5.6-t2v",
            "pixverse-v5.6-i2v",
            "pixverse-v5.6-transition",
        ):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "gmi-video-pixverse", slug
            # Pixverse needs ``quality`` in the allowlist.
            assert "quality" in (match.spec.param_allowlist or set())

    def test_wan_r2v_routes_to_wan_r2v_family(self) -> None:
        provider = GMICloudVideoProvider(api_key="test")
        match = provider._models.match_family("wan2.6-r2v")
        assert match is not None
        assert match.family.name == "gmi-video-wan-r2v"
        # Wan-r2v needs keyframe references in the allowlist.
        assert "image_url" in (match.spec.param_allowlist or set())
        assert "tail_image_url" in (match.spec.param_allowlist or set())

    def test_veo_routes_to_veo_family_with_has_audio(self) -> None:
        """Veo slugs match the dedicated family that carries
        ``extras["has_audio"]=True`` — that's how ``fetch_output``
        attaches audio metadata without a parallel slug list."""
        provider = GMICloudVideoProvider(api_key="test")
        for slug in ("veo3", "veo3-fast"):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "gmi-video-veo", slug
            assert match.spec.extras.get("has_audio") is True, slug

    def test_other_video_slugs_fall_through_to_fallback(self) -> None:
        """Slugs that don't match a specialized family (Pixverse, Wan-r2v,
        Veo, Kling V2.1) fall through to the permissive fallback. The base
        video surface (``cfg_scale`` alias, ``duration`` coercion) lives
        on the fallback spec, not on a catch-all family."""
        provider = GMICloudVideoProvider(api_key="test")
        for slug in (
            "seedance-1-0-pro-250528",
            "wan2.6-t2v",
            "luma-ray-2",
            # Newer Kling V2.5/V3 series uses lowercase; no dedicated
            # family ships their param surface — they hit the fallback.
            "kling-v3-text-to-video",
        ):
            match = provider._models.match_family(slug)
            assert match is None, slug
            # ``get`` still returns a usable spec via the fallback.
            spec = provider._models.get(slug)
            assert "cfg_scale" in spec.param_aliases.values()
            assert "duration" in spec.param_coercers
            assert "duration" in spec.param_schemas

    def test_kling_v21_routes_to_dedicated_family(self) -> None:
        """Kling V2.1 (text2video + image2video) gets its own family
        because GMI's wire form is PascalCase and the family carries
        ``canonical_slug`` to rewrite lowercase user input."""
        provider = GMICloudVideoProvider(api_key="test")
        for slug, expected_wire in (
            ("kling-text2video-v2.1-master", "Kling-Text2Video-V2.1-Master"),
            ("Kling-Text2Video-V2.1-Master", "Kling-Text2Video-V2.1-Master"),
            ("kling-image2video-v2.1-master", "Kling-Image2Video-V2.1-Master"),
            ("Kling-Image2Video-V2.1-Master", "Kling-Image2Video-V2.1-Master"),
        ):
            match = provider._models.match_family(slug)
            assert match is not None, slug
            assert match.family.name == "gmi-video-kling-v21", slug
            assert match.spec.model_id == expected_wire, slug


# --- unstable_examples propagation (RT-10) ---------------------------------


class TestUnstableExamples:
    """0.3.2 cleanup: most slugs previously flagged ``suspected_dead`` in
    the 2026-04 reconciliation were actually *case-variant* mismatches.
    GMI's published catalog confirms the lowercase audio family slugs
    and PascalCase Kling V2.1 / Veo3 slugs are live. ``canonical_slug``
    now rewrites pre-0.3.2 callers to the wire form; only genuinely
    rotated-out slugs (e.g. ``vidu-q1``) remain flagged."""

    def test_orphan_unstable_slug_via_registry(self) -> None:
        """``vidu-q1`` was replaced by ``vidu-q3-pro-i2v`` per GMI's
        2026-03-04 catalog blog; it stays flagged at the registry level
        as a permissive-fallback ``known_unstable`` hint until a
        maintainer confirms via the probe tool."""
        provider = GMICloudVideoProvider(api_key="test")
        result = provider._models.validate("vidu-q1")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE
        assert "known_unstable" in (result.detail or "")

    def test_live_video_slug_no_unstable_hint(self) -> None:
        """A slug that's neither in a family's ``unstable_examples`` nor
        in ``unstable_slugs`` should NOT carry the known_unstable detail."""
        provider = GMICloudVideoProvider(api_key="test")
        # seedance — no family match, not in unstable_slugs
        result = provider._models.validate("seedance-1-0-pro-250528")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE
        assert "known_unstable" not in (result.detail or "")
        # Veo3 — matches Veo family (canonical PascalCase form)
        result = provider._models.validate("Veo3")
        assert result.outcome is ValidationOutcome.OK_PROVISIONAL
        assert "known_unstable" not in (result.detail or "")


# --- empty_payload_request_probe primitive ---------------------------------


def _http_with_status(status: int) -> MagicMock:
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    http.post.return_value = resp
    return http


class TestEmptyPayloadRequestProbe:
    def test_404_means_dead(self) -> None:
        result = empty_payload_request_probe("Dead-Slug", http=_http_with_status(404))
        assert result is LiveProbeResult.DEAD

    def test_400_means_live(self) -> None:
        result = empty_payload_request_probe("Live-Slug", http=_http_with_status(400))
        assert result is LiveProbeResult.LIVE

    def test_2xx_means_live(self) -> None:
        result = empty_payload_request_probe("Live-Slug", http=_http_with_status(200))
        assert result is LiveProbeResult.LIVE

    def test_429_inconclusive(self) -> None:
        result = empty_payload_request_probe("Slug", http=_http_with_status(429))
        assert result is LiveProbeResult.UNKNOWN

    def test_5xx_inconclusive(self) -> None:
        result = empty_payload_request_probe("Slug", http=_http_with_status(503))
        assert result is LiveProbeResult.UNKNOWN

    def test_transport_error_inconclusive(self) -> None:
        http = MagicMock()
        http.post.side_effect = RuntimeError("network error")
        result = empty_payload_request_probe("Slug", http=http)
        assert result is LiveProbeResult.UNKNOWN

    def test_probe_posts_to_requests_with_envelope(self) -> None:
        """Confirm the probe POSTs to /requests with the GMI envelope —
        not a different path or payload shape."""
        http = _http_with_status(400)
        empty_payload_request_probe("MiniMax-TTS", http=http)
        http.post.assert_called_once_with(
            "/requests", json={"model": "MiniMax-TTS", "payload": {}}
        )


# --- validate_model end-to-end via the family probe ------------------------


def _provider_with_probe_status(cls: type, status: int) -> object:
    provider = cls(api_key="test", http_client=_http_with_status(status))
    return provider


class TestValidateModelEndToEnd:
    def test_dead_audio_slug_surfaces_not_found(self) -> None:
        """The reconciliation's headline case: an unstable_examples slug
        that the probe confirms is dead → preflight raises NOT_FOUND."""
        provider = _provider_with_probe_status(GMICloudAudioProvider, status=404)
        result = provider.validate_model("ElevenLabs-TTS-v3")
        assert result.outcome is ValidationOutcome.NOT_FOUND
        assert result.source is ValidationSource.PROBE

    def test_live_audio_slug_authoritative(self) -> None:
        provider = _provider_with_probe_status(GMICloudAudioProvider, status=400)
        result = provider.validate_model("MiniMax-TTS-Speech-2.6-Turbo")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.PROBE

    def test_live_image_slug_authoritative(self) -> None:
        provider = _provider_with_probe_status(GMICloudImageProvider, status=400)
        result = provider.validate_model("bria-genfill")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE

    def test_live_video_pixverse_authoritative(self) -> None:
        provider = _provider_with_probe_status(GMICloudVideoProvider, status=400)
        result = provider.validate_model("pixverse-v5.6-t2v")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE

    def test_unknown_namespace_falls_through_permissive(self) -> None:
        """A slug that doesn't match any family AND no probe attached
        falls through to UNKNOWN_PERMISSIVE — preflight emits a one-time
        WARN and proceeds."""
        # Audio families don't cover lowercase slugs; this passes through.
        provider = _provider_with_probe_status(GMICloudAudioProvider, status=404)
        result = provider.validate_model("totally-unknown-tts-slug")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


# --- Probe cache (BaseProvider) -------------------------------------------


class TestProbeCache:
    """Verify the per-slug probe cache + single-flight on BaseProvider.

    The probe cache is the cost-control mechanism for queue-style
    PARTIAL providers (GMI, Runway, Luma) where each probe creates an
    audit-log entry on the user's account. These tests pin the
    documented contract.
    """

    def test_warm_cache_skips_probe_invocation(self) -> None:
        """Successive ``validate_model`` calls for the same slug within
        the TTL must NOT re-fire the probe."""
        http = _http_with_status(400)  # LIVE
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        provider.validate_model("pixverse-v5.6-t2v")
        provider.validate_model("pixverse-v5.6-t2v")
        provider.validate_model("pixverse-v5.6-t2v")
        # Three calls, one probe — http.post fired exactly once.
        assert http.post.call_count == 1

    def test_refresh_evicts_cache(self) -> None:
        """``refresh=True`` must re-fire the probe even if cached."""
        http = _http_with_status(400)
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        provider.validate_model("pixverse-v5.6-t2v")
        provider.validate_model("pixverse-v5.6-t2v", refresh=True)
        assert http.post.call_count == 2

    def test_distinct_slugs_each_probe(self) -> None:
        """Different slugs produce distinct cache entries."""
        http = _http_with_status(400)
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        provider.validate_model("pixverse-v5.6-t2v")
        provider.validate_model("pixverse-v5.6-i2v")
        provider.validate_model("wan2.6-r2v")
        assert http.post.call_count == 3

    def test_unknown_result_not_cached(self) -> None:
        """A 5xx upstream returns LiveProbeResult.UNKNOWN; the cache
        should NOT memoize it (transient errors deserve re-checking)."""
        http = _http_with_status(503)
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        provider.validate_model("pixverse-v5.6-t2v")
        provider.validate_model("pixverse-v5.6-t2v")
        # Both calls fired — UNKNOWN is not cached.
        assert http.post.call_count == 2

    def test_concurrent_callers_share_one_probe(self) -> None:
        """Single-flight: 50 threads racing on the same slug fire ONE
        probe, not 50. Bounds audit-log noise on cold-burst preflight."""
        import threading
        import time

        # Use an event-gated http client that blocks the first POST so
        # all 50 threads enter the cache-miss path before any of them
        # gets a response.
        gate = threading.Event()
        post_count = [0]
        post_lock = threading.Lock()

        def slow_post(*a, **k):
            with post_lock:
                post_count[0] += 1
            gate.wait(timeout=2.0)
            resp = MagicMock(status_code=400)
            return resp

        http = MagicMock()
        http.post.side_effect = slow_post
        provider = GMICloudVideoProvider(api_key="test", http_client=http)

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(provider.validate_model("pixverse-v5.6-t2v"))
            )
            for _ in range(50)
        ]
        for t in threads:
            t.start()
        time.sleep(0.05)  # let threads enqueue
        gate.set()
        for t in threads:
            t.join(timeout=5.0)

        assert post_count[0] == 1, (
            f"single-flight failed: {post_count[0]} probes across 50 threads"
        )
        assert len(results) == 50
        assert all(r.outcome is ValidationOutcome.OK_AUTHORITATIVE for r in results)

    def test_cache_eviction_under_size_pressure(self) -> None:
        """Cache stays bounded under probe_cache_max_entries — daemons
        that see many distinct slugs don't grow the cache unbounded."""
        http = _http_with_status(400)
        # Tighten the cap via the constructor kwarg so the test doesn't
        # have to fire 256+ probes to exercise eviction.
        provider = GMICloudVideoProvider(
            api_key="test", http_client=http, probe_cache_max_entries=8
        )
        # Use family-matched slugs (Pixverse pattern). Exhaust the cap.
        for i in range(20):
            provider.validate_model(f"pixverse-v5.6-t2v-variant-{i}")
        assert len(provider._probe_cache) <= 8


# --- Pipeline preflight opt-out -------------------------------------------


class TestPreflightOptOut:
    """Verify ``Pipeline(preflight=False)`` truly skips the probe path —
    no audit-log noise for users who opted out."""

    def test_preflight_false_skips_probe(self) -> None:
        from genblaze_core import Pipeline
        from genblaze_core.models.enums import Modality

        http = _http_with_status(404)  # would fail loudly if probe ran
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        pipe = Pipeline("opt-out", preflight=False).step(
            provider,
            model="veo3-fast",  # known-unstable; probe would say DEAD
            modality=Modality.VIDEO,
            prompt="test",
        )
        # _validate_steps runs at run() start; with preflight=False it
        # should NOT issue any HTTP calls.
        pipe._validate_steps()
        assert http.post.call_count == 0, (
            f"preflight=False should skip the probe; saw {http.post.call_count} HTTP calls"
        )

    def test_preflight_true_default_does_probe(self) -> None:
        """Sanity: with the default preflight=True, the probe DOES fire."""
        from genblaze_core import Pipeline
        from genblaze_core.models.enums import Modality

        http = _http_with_status(400)  # LIVE
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        pipe = Pipeline("default-on").step(
            provider,
            model="pixverse-v5.6-t2v",
            modality=Modality.VIDEO,
            prompt="test",
        )
        pipe._validate_steps()
        assert http.post.call_count == 1


# --- Probe-LIVE preserves known_unstable detail (RT-7) -------------------


class TestUnstableSlugSurfaces:
    """0.3.2 cleanup: the only remaining ``unstable_slug`` is the orphan
    ``vidu-q1`` (no family, registry-level). It surfaces as
    ``UNKNOWN_PERMISSIVE`` + ``known_unstable`` via the fallback path —
    there's no family probe to upgrade it to ``OK_AUTHORITATIVE``."""

    def test_orphan_unstable_slug_surfaces_known_unstable(self) -> None:
        http = _http_with_status(400)  # LIVE — irrelevant; no family probe runs
        provider = GMICloudVideoProvider(api_key="test", http_client=http)
        result = provider.validate_model("vidu-q1")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE
        assert "known_unstable" in (result.detail or "")
