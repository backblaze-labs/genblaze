"""Coverage for the Phase 0/3 standardization hooks on GMI providers.

Specifically validates:
- ``GMICloudBase.__init__`` forwards the ``models=`` kwarg (closes P0-03).
- ``preflight_auth`` short-circuits on credentials and raises
  ``ProviderError(AUTH_FAILURE)`` on 401/403.
- ``probe_model`` distinguishes 404 (NOT_FOUND) from 400 (OK).
- ``list_voices`` returns curated ``Voice`` objects from the static catalog.
- ``estimate_cost`` works without a network call (used by Pipeline.estimated_cost).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.voice import Voice
from genblaze_core.providers import ProbeStatus
from genblaze_gmicloud import (
    GMICloudAudioProvider,
    GMICloudImageProvider,
    GMICloudVideoProvider,
)

# --- models= kwarg forwarding -----------------------------------------------


@pytest.mark.parametrize(
    "cls", [GMICloudVideoProvider, GMICloudImageProvider, GMICloudAudioProvider]
)
def test_models_kwarg_forwards_to_base(cls):
    custom = cls.models_default().fork()
    provider = cls(api_key="test", models=custom)
    assert provider.models is custom


# --- preflight_auth ---------------------------------------------------------


def test_preflight_raises_on_401(monkeypatch):
    """Bad credentials surface as AUTH_FAILURE in <5s, not as a 120s submit hang."""
    fake_resp = MagicMock(status_code=401, text='{"error":"invalid token"}')
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = fake_resp

    monkeypatch.delenv("GENBLAZE_SKIP_PREFLIGHT", raising=False)
    with patch.object(httpx, "Client", return_value=fake_client):
        provider = GMICloudVideoProvider(api_key="bogus")
        with pytest.raises(ProviderError) as exc:
            provider.preflight_auth(timeout=1)
    assert exc.value.error_code == ProviderErrorCode.AUTH_FAILURE


def test_preflight_silent_on_200(monkeypatch):
    fake_resp = MagicMock(status_code=200, text="{}")
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = fake_resp

    monkeypatch.delenv("GENBLAZE_SKIP_PREFLIGHT", raising=False)
    with patch.object(httpx, "Client", return_value=fake_client):
        provider = GMICloudVideoProvider(api_key="ok")
        provider.preflight_auth(timeout=1)  # no raise == pass


def test_preflight_swallows_network_errors(monkeypatch):
    """Transient network errors during preflight must not block submit."""
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.side_effect = httpx.ConnectError("net down")

    monkeypatch.delenv("GENBLAZE_SKIP_PREFLIGHT", raising=False)
    with patch.object(httpx, "Client", return_value=fake_client):
        provider = GMICloudVideoProvider(api_key="ok")
        provider.preflight_auth(timeout=1)  # no raise


# --- probe_model (deprecated adapter — slated for removal in 0.4.0) ---------
#
# As of genblaze-core 0.3.0 the legacy ``probe_model`` method is a thin
# adapter over ``validate_model(refresh=True)`` that coerces the
# ``ValidationResult`` to a ``ProbeResult``. The actual liveness check
# runs via the family-attached ``empty_payload_request_probe`` and goes
# through ``BaseProvider``'s shared probe cache. These tests verify the
# adapter's coerced outcomes end-to-end. New code should call
# ``validate_model()`` directly.


def test_probe_model_returns_not_found_on_404():
    """Family-matched slug + 404 → probe DEAD → NOT_FOUND."""
    import warnings

    fake_resp = MagicMock(status_code=404, text='{"error":"unknown model"}')
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    provider = GMICloudVideoProvider(api_key="ok", http_client=fake_client)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # ``veo3-fast`` matches the gmi-video-veo family and triggers
        # the empty-payload probe.
        result = provider.probe_model("veo3-fast")
    assert result.status is ProbeStatus.NOT_FOUND


def test_probe_model_returns_ok_on_400():
    """Family-matched slug + 400 → probe LIVE (model accepted, payload
    rejected) → OK_AUTHORITATIVE → ProbeStatus.OK."""
    import warnings

    fake_resp = MagicMock(status_code=400, text='{"error":"missing prompt"}')
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    provider = GMICloudVideoProvider(api_key="ok", http_client=fake_client)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # ``pixverse-v5.6-t2v`` matches the gmi-video-pixverse family.
        result = provider.probe_model("pixverse-v5.6-t2v")
    assert result.status is ProbeStatus.OK


def test_probe_model_returns_unknown_on_401():
    """Family-matched slug + 401/403 → probe UNKNOWN → falls through to
    OK_PROVISIONAL → coerced to ProbeStatus.UNKNOWN.

    Auth failures don't indicate slug deadness — they tell us we can't
    determine liveness. The legacy ``ProbeStatus.AUTH`` was removed
    because it conflated "I don't have permission to ask" with "the
    slug is gone"; UNKNOWN is the honest answer.
    """
    import warnings

    fake_resp = MagicMock(status_code=401, text='{"error":"bad key"}')
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    provider = GMICloudVideoProvider(api_key="bogus", http_client=fake_client)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        result = provider.probe_model("veo3-fast")
    assert result.status is ProbeStatus.UNKNOWN


# --- list_voices ------------------------------------------------------------


def test_list_voices_returns_curated_catalog():
    provider = GMICloudAudioProvider(api_key="ok")
    voices = provider.list_voices()
    assert len(voices) > 0
    assert all(isinstance(v, Voice) for v in voices)
    assert all(v.provider == "gmicloud-audio" for v in voices)


def test_list_voices_filters_by_model():
    provider = GMICloudAudioProvider(api_key="ok")
    voices = provider.list_voices(model="ElevenLabs-TTS-v3")
    assert len(voices) > 0
    assert all(v.model == "ElevenLabs-TTS-v3" for v in voices)


def test_list_voices_filters_by_language_prefix():
    provider = GMICloudAudioProvider(api_key="ok")
    en_voices = provider.list_voices(language="en")
    assert len(en_voices) > 0
    assert all(v.language and v.language.startswith("en") for v in en_voices)


# --- estimate_cost ---------------------------------------------------------


def test_estimate_cost_returns_none_without_user_pricing():
    """As of genblaze-core 0.3.0 the SDK ships zero hardcoded prices.
    ``estimate_cost`` returns ``None`` for any model unless the user
    has registered a pricing strategy via ``register_pricing()``.
    See ``docs/reference/pricing-recipes.md``.
    """
    provider = GMICloudVideoProvider(api_key="ok")
    cost = provider.estimate_cost("seedance-1-0-pro-250528")
    assert cost is None


def test_estimate_cost_with_user_registered_pricing():
    """User-registered pricing flows through the same estimate_cost path."""
    from genblaze_core.providers import per_unit

    provider = GMICloudVideoProvider(api_key="ok")
    # Fork to avoid polluting the class-level models_default() cache.
    provider._models = provider.models.fork()
    provider.models.register_pricing("seedance-1-0-pro-250528", per_unit(0.30))
    cost = provider.estimate_cost("seedance-1-0-pro-250528")
    assert cost == Decimal("0.30")
