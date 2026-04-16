"""Tests for provider entry point discovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from genblaze_core.providers.base import BaseProvider
from genblaze_core.providers.registry import discover_providers

_EP_PATH = "genblaze_core.providers.registry.importlib.metadata.entry_points"


class _FakeProvider(BaseProvider):
    name = "fake"

    def submit(self, step, config=None):
        return "id"

    def poll(self, prediction_id, config=None):
        return True

    def fetch_output(self, prediction_id, step):
        return step


def _make_entry_point(name: str, cls):
    """Create a mock entry point that loads to cls."""
    ep = MagicMock()
    ep.name = name
    ep.load.return_value = cls
    return ep


def test_discover_finds_registered_providers() -> None:
    ep = _make_entry_point("fake", _FakeProvider)

    with patch(_EP_PATH, return_value=[ep]):
        result = discover_providers()

    assert "fake" in result
    assert result["fake"] is _FakeProvider


def test_discover_empty_when_none_installed() -> None:
    with patch(_EP_PATH, return_value=[]):
        result = discover_providers()

    assert result == {}


def test_discover_skips_non_provider_class() -> None:
    """Entry points that don't resolve to BaseProvider subclass are skipped."""
    ep = _make_entry_point("bad", str)

    with patch(_EP_PATH, return_value=[ep]):
        result = discover_providers()

    assert result == {}


def test_discover_handles_load_error() -> None:
    """Entry points that fail to load are skipped gracefully."""
    ep = MagicMock()
    ep.name = "broken"
    ep.load.side_effect = ImportError("missing package")

    with patch(_EP_PATH, return_value=[ep]):
        result = discover_providers()

    assert result == {}
