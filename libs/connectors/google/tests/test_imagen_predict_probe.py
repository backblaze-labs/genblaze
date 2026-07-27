"""Tests for google_imagen_predict_probe (issue #206).

`models.get` proves catalog membership, not entitlement: on a new Gemini
key every `imagen-4.0-*` slug is catalog-listed (models.get 200) but 404s
on the `:predict` call ImagenProvider makes. The predict-probe adds one
deliberately-invalid `generate_images(prompt="")` call and treats a 404
there as the entitlement gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from genblaze_core.providers import LiveProbeResult
from genblaze_google._probe import google_imagen_predict_probe

_SLUG = "imagen-4.0-generate-001"


def _client(*, get_error=None, predict_error=None):
    """Mock genai client. ``get_error``/``predict_error`` (if set) are raised
    by ``models.get`` / ``models.generate_images`` respectively."""
    client = MagicMock()
    if get_error is not None:
        client.models.get.side_effect = get_error
    if predict_error is not None:
        client.models.generate_images.side_effect = predict_error
    return client


def test_dead_when_catalog_live_but_predict_404s():
    """The core #206 case: models.get 200 (LIVE) but :predict 404s
    'no longer available to new users' -> DEAD at preflight, not mid-run."""
    client = _client(
        predict_error=RuntimeError(
            "404 This model models/imagen-4.0-generate-001 is no longer available to new users."
        )
    )
    assert google_imagen_predict_probe(_SLUG, client=client) is LiveProbeResult.DEAD


def test_live_when_predict_succeeds():
    client = _client()  # both calls succeed
    assert google_imagen_predict_probe(_SLUG, client=client) is LiveProbeResult.LIVE


def test_live_when_predict_fails_inconclusively():
    """A 400 from our own deliberately-empty prompt (or any non-404) is not
    evidence against the catalog-membership LIVE — the slug stays LIVE."""
    client = _client(predict_error=RuntimeError("400 invalid argument: empty prompt"))
    assert google_imagen_predict_probe(_SLUG, client=client) is LiveProbeResult.LIVE


def test_short_circuits_without_predict_when_models_get_dead():
    """If models.get itself 404s, the slug is absent — return DEAD without
    ever making the extra :predict probe call."""
    client = _client(get_error=RuntimeError("404 NOT_FOUND"))
    assert google_imagen_predict_probe(_SLUG, client=client) is LiveProbeResult.DEAD
    client.models.generate_images.assert_not_called()


def test_short_circuits_without_predict_when_models_get_unknown():
    """An inconclusive models.get (auth/transport) is UNKNOWN and conclusive
    for the probe — no extra :predict call."""
    client = _client(get_error=RuntimeError("401 unauthorized"))
    assert google_imagen_predict_probe(_SLUG, client=client) is LiveProbeResult.UNKNOWN
    client.models.generate_images.assert_not_called()
