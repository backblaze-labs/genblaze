"""Tests for GMICloudBase HTTP client lifecycle and base-URL handling."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import httpx
from genblaze_gmicloud import GMICloudVideoProvider
from genblaze_gmicloud._base import _DEFAULT_BASE_URL


def test_default_base_url_when_no_override():
    """Absent env and ctor override, the production URL is used."""
    saved = os.environ.pop("GMI_BASE_URL", None)
    try:
        p = GMICloudVideoProvider(api_key="test-key")
        assert p._base_url == _DEFAULT_BASE_URL
    finally:
        if saved is not None:
            os.environ["GMI_BASE_URL"] = saved


def test_ctor_base_url_wins_over_env(monkeypatch):
    monkeypatch.setenv("GMI_BASE_URL", "https://env.example/v1")
    p = GMICloudVideoProvider(api_key="test-key", base_url="https://ctor.example/v1")
    assert p._base_url == "https://ctor.example/v1"


def test_env_base_url_used_when_ctor_not_set(monkeypatch):
    monkeypatch.setenv("GMI_BASE_URL", "https://env.example/v1")
    p = GMICloudVideoProvider(api_key="test-key")
    assert p._base_url == "https://env.example/v1"


def test_internally_created_client_uses_configured_base_url(monkeypatch):
    """First real HTTP attempt should build the client against our base_url."""
    monkeypatch.setenv("GMI_BASE_URL", "https://env.example/v1")
    p = GMICloudVideoProvider(api_key="test-key")
    client = p._get_http_client()
    try:
        assert str(client.base_url).rstrip("/") == "https://env.example/v1"
    finally:
        p.close()


def test_external_client_injected_is_not_closed():
    """Caller-supplied clients must outlive the provider's close()."""
    external = MagicMock(spec=httpx.Client)
    p = GMICloudVideoProvider(api_key="test-key", http_client=external)
    assert p._owns_client is False
    assert p._get_http_client() is external
    p.close()
    external.close.assert_not_called()


def test_internally_created_client_is_closed_on_close():
    p = GMICloudVideoProvider(api_key="test-key")
    real_client = p._get_http_client()
    assert p._owns_client is True
    p.close()
    # Closed client raises on further use — observable close signal.
    assert real_client.is_closed is True


def test_external_client_bypasses_api_key_requirement():
    """No env var, no api_key, but external client → no AUTH_FAILURE."""
    saved = os.environ.pop("GMI_API_KEY", None)
    try:
        external = MagicMock(spec=httpx.Client)
        p = GMICloudVideoProvider(http_client=external)
        # Should not raise; client is already configured with auth.
        assert p._get_http_client() is external
    finally:
        if saved is not None:
            os.environ["GMI_API_KEY"] = saved
