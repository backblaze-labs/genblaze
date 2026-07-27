"""Regression tests for #193 item 1: GMI_BASE_URL is queue-specific.

``GMI_BASE_URL`` reads as a general GMICloud override but is only ever
consulted by ``GMICloudBase`` (the video/image/audio queue providers) —
``chat()`` has its own hardcoded default and never reads it. Pointing it
at GMICloud's chat/inference endpoint (``https://api.gmi-serving.com/v1``)
silently 404s every media model while chat() keeps working, which looks
exactly like an entitlement problem. ``GMICloudBase.__init__`` now rejects
any ``/v1``-shaped base_url/env-var with a clear, actionable error.
"""

from __future__ import annotations

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_gmicloud import GMICloudAudioProvider, GMICloudImageProvider, GMICloudVideoProvider


class TestBaseUrlGuardRejectsInferenceShape:
    def test_ctor_base_url_ending_in_v1_rejected(self):
        with pytest.raises(ProviderError) as exc_info:
            GMICloudVideoProvider(api_key="test", base_url="https://api.gmi-serving.com/v1")
        assert exc_info.value.error_code is ProviderErrorCode.INVALID_INPUT
        assert "base_url=" in str(exc_info.value)
        assert "chat" in str(exc_info.value).lower()

    def test_env_base_url_ending_in_v1_rejected(self, monkeypatch):
        monkeypatch.setenv("GMI_BASE_URL", "https://api.gmi-serving.com/v1")
        with pytest.raises(ProviderError) as exc_info:
            GMICloudImageProvider(api_key="test")
        assert exc_info.value.error_code is ProviderErrorCode.INVALID_INPUT
        assert "GMI_BASE_URL" in str(exc_info.value)

    def test_trailing_slash_v1_also_rejected(self):
        """A trailing slash shouldn't let the /v1 shape slip past the guard."""
        with pytest.raises(ProviderError):
            GMICloudAudioProvider(api_key="test", base_url="https://api.gmi-serving.com/v1/")

    def test_applies_across_all_three_modalities(self):
        for cls in (GMICloudVideoProvider, GMICloudImageProvider, GMICloudAudioProvider):
            with pytest.raises(ProviderError):
                cls(api_key="test", base_url="https://api.gmi-serving.com/v1")


class TestBaseUrlGuardAllowsQueueShapes:
    def test_default_queue_url_not_rejected(self):
        """The real default (.../v1/ie/requestqueue/apikey) must never trip
        the guard — it has extra path segments after /v1."""
        GMICloudVideoProvider(api_key="test")  # no raise

    def test_custom_queue_proxy_url_not_rejected(self):
        GMICloudVideoProvider(
            api_key="test", base_url="https://my-vpc-proxy.example/gmi/v1/ie/requestqueue"
        )  # no raise

    def test_http_client_injection_bypasses_the_guard(self):
        """base_url is documented as ignored when http_client is supplied —
        the guard must not fire even if GMI_BASE_URL is set to a /v1 shape,
        since it's genuinely not consulted in that path."""
        import httpx

        client = httpx.Client(base_url="https://api.gmi-serving.com/v1")
        try:
            GMICloudVideoProvider(http_client=client)  # no raise
        finally:
            client.close()
