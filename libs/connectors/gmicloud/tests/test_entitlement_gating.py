"""Regression tests for #193 item 3: validate vs dispatch.

``validate_model()`` passing does not mean the API key can run the model.
``seededit-3-0-i2i-250628`` matches ``gmi-image-edit`` and GMICloud's
empty-payload probe reports it LIVE (the slug exists), but a real submit
404s "you do not have access" for accounts without extra entitlement —
preflight passed and the job died at dispatch.

The probe structurally can't detect this: GMICloud validates payload
*shape* before account *entitlement*, so the probe's deliberately-empty
payload never reaches the entitlement gate a real, well-formed submit
hits. ``GMICloudBase.validate_model()`` re-grades known-gated slugs from
``OK_AUTHORITATIVE`` to ``OK_PROVISIONAL`` so "known slug" is
distinguishable from "confirmed callable with this key" in the returned
``ValidationResult`` — see ``GMICloudBase._entitlement_gated_slugs``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from genblaze_core.providers import ValidationOutcome, ValidationSource
from genblaze_gmicloud import GMICloudImageProvider


def _http_with_status(status: int) -> MagicMock:
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    http.post.return_value = resp
    return http


class TestKnownGatedSlugDistinguishableFromCallable:
    def test_gated_slug_downgraded_to_provisional(self):
        """The probe still says LIVE (400 == known/malformed-payload), but
        the connector knows this specific slug requires extra GMICloud
        entitlement, so the outcome is downgraded from OK_AUTHORITATIVE to
        OK_PROVISIONAL — no longer indistinguishable from a truly-confirmed
        slug."""
        provider = GMICloudImageProvider(api_key="test", http_client=_http_with_status(400))
        result = provider.validate_model("seededit-3-0-i2i-250628")
        assert result.outcome is ValidationOutcome.OK_PROVISIONAL
        assert result.source is ValidationSource.PROBE
        assert "not confirmed callable" in (result.detail or "").lower()
        assert "seededit-3-0-i2i-250628" in (result.detail or "")

    def test_ungated_slug_in_same_family_stays_authoritative(self):
        """reve-edit shares the gmi-image-edit family with seededit but
        isn't flagged as gated — it must keep the full OK_AUTHORITATIVE
        confidence grade. Proves the fix is scoped to the specific known
        slug, not the whole family."""
        provider = GMICloudImageProvider(api_key="test", http_client=_http_with_status(400))
        result = provider.validate_model("reve-edit-20250915")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE

    def test_gated_slug_distinguishable_from_dead_slug(self):
        """A gated-but-known slug (OK_PROVISIONAL) must not collapse into
        the same verdict as a genuinely dead slug (NOT_FOUND) — the two
        failure modes need different operator responses."""
        provider = GMICloudImageProvider(api_key="test", http_client=_http_with_status(400))
        gated = provider.validate_model("seededit-3-0-i2i-250628")

        dead_provider = GMICloudImageProvider(api_key="test", http_client=_http_with_status(404))
        dead = dead_provider.validate_model("seededit-3-0-i2i-250628")

        assert gated.outcome is not dead.outcome
        assert gated.outcome is ValidationOutcome.OK_PROVISIONAL
        assert dead.outcome is ValidationOutcome.NOT_FOUND

    def test_gated_slug_distinguishable_from_confirmed_callable_slug(self):
        """The core ask from #193: a known-but-unentitled slug must not
        produce the same ValidationResult.outcome as a slug the connector
        can actually confirm is callable."""
        provider = GMICloudImageProvider(api_key="test", http_client=_http_with_status(400))
        gated = provider.validate_model("seededit-3-0-i2i-250628")
        callable_slug = provider.validate_model("bria-genfill")

        assert gated.outcome is ValidationOutcome.OK_PROVISIONAL
        assert callable_slug.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert gated.outcome is not callable_slug.outcome

    def test_gated_slug_probe_still_only_fires_once_per_ttl(self):
        """The re-grading happens after the (cached, single-flight) probe
        call — it must not cause extra HTTP round-trips."""
        http = _http_with_status(400)
        provider = GMICloudImageProvider(api_key="test", http_client=http)
        provider.validate_model("seededit-3-0-i2i-250628")
        provider.validate_model("seededit-3-0-i2i-250628")
        assert http.post.call_count == 1


class TestNoEntitlementGatingOnOtherModalities:
    """Only GMICloudImageProvider ships a known-gated slug today — video
    and audio default to an empty gate set and must be unaffected."""

    def test_video_provider_has_no_gated_slugs_by_default(self):
        from genblaze_gmicloud import GMICloudVideoProvider

        assert GMICloudVideoProvider._entitlement_gated_slugs == frozenset()

    def test_audio_provider_has_no_gated_slugs_by_default(self):
        from genblaze_gmicloud import GMICloudAudioProvider

        assert GMICloudAudioProvider._entitlement_gated_slugs == frozenset()
