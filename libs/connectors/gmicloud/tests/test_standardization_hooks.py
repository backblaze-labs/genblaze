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


# --- probe_model ------------------------------------------------------------


def test_probe_model_returns_not_found_on_404():
    fake_resp = MagicMock(status_code=404, text='{"error":"unknown model"}')
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    provider = GMICloudVideoProvider(api_key="ok", http_client=fake_client)
    result = provider.probe_model("nonexistent-slug")
    assert result.status is ProbeStatus.NOT_FOUND


def test_probe_model_returns_ok_on_400():
    """``400`` means model accepted but our empty payload was bad — model is alive."""
    fake_resp = MagicMock(status_code=400, text='{"error":"missing prompt"}')
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    provider = GMICloudVideoProvider(api_key="ok", http_client=fake_client)
    result = provider.probe_model("seedance-1-0-pro-250528")
    assert result.status is ProbeStatus.OK


def test_probe_model_returns_auth_on_401():
    fake_resp = MagicMock(status_code=401, text='{"error":"bad key"}')
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    provider = GMICloudVideoProvider(api_key="bogus", http_client=fake_client)
    result = provider.probe_model("any")
    assert result.status is ProbeStatus.AUTH


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


def test_estimate_cost_no_network():
    provider = GMICloudVideoProvider(api_key="ok")
    cost = provider.estimate_cost("seedance-1-0-pro-250528")
    assert cost == Decimal("0.30")


def test_estimate_cost_per_second_with_duration():
    provider = GMICloudVideoProvider(api_key="ok")
    # seedance-2-0-260128 is per-second priced @ 0.052/s.
    cost = provider.estimate_cost("seedance-2-0-260128", params={"duration": 10})
    assert cost == Decimal("0.52")
