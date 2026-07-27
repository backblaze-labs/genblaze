"""Tests for ImagenProvider entitlement re-grading (issue #206, #220 panel).

`imagen-4.0-*` slugs are catalog-listed (`models.get` 200 -> probe LIVE) but
404 "no longer available to new users" on the `:predict` call for keys
created after the imagen 3.0->4.0 migration. `validate_model` re-grades those
to `OK_PROVISIONAL` (a warn) rather than a billable `:predict` probe (spend
risk) or a hard `DEAD` (would lock out an entitled user on any unrelated 404).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.providers import ValidationOutcome


@pytest.fixture
def imagen_provider():
    """ImagenProvider wired to a mock client whose `models.get` succeeds
    (so the catalog probe reports LIVE -> base validate_model returns
    OK_AUTHORITATIVE via the PROBE source)."""
    mock_types = MagicMock()
    mock_genai = MagicMock()
    mock_genai.types = mock_types
    mock_google = MagicMock()
    mock_google.genai = mock_genai
    with patch.dict(
        "sys.modules",
        {"google": mock_google, "google.genai": mock_genai, "google.genai.types": mock_types},
    ):
        from genblaze_google.imagen import ImagenProvider

        client = MagicMock()
        client.models.get.return_value = MagicMock()  # catalog membership: LIVE
        provider = ImagenProvider(api_key="test-key")
        provider._client = client
        yield provider


def test_entitlement_gated_imagen_4_regraded_provisional(imagen_provider):
    result = imagen_provider.validate_model("imagen-4.0-generate-001")
    assert result.outcome is ValidationOutcome.OK_PROVISIONAL
    # the detail explains the entitlement gate + points at the Gemini path
    assert "entitlement" in (result.detail or "").lower()
    assert "google-gemini-image" in (result.detail or "")


def test_non_gated_imagen_stays_authoritative(imagen_provider):
    """Only the known-gated 4.0 line is re-graded; a catalog-LIVE non-4.0
    imagen slug keeps the authoritative verdict."""
    result = imagen_provider.validate_model("imagen-5.0-generate-001")
    assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE


def test_validate_makes_no_billable_generation_call(imagen_provider):
    """Regression for the #220 panel finding: preflight validation must never
    hit the generation endpoint — that risked a real, billable Imagen call on
    an entitled account."""
    imagen_provider.validate_model("imagen-4.0-generate-001")
    imagen_provider._client.models.generate_images.assert_not_called()
