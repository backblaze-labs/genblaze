"""Shared family-probe primitive for Google Veo / Imagen.

google-genai exposes ``client.models.get(model=...)`` which is the
canonical "is this slug available to my project?" lookup. It hits the
Gemini API (or Vertex AI) without enqueuing a generation, so it's safe
to call from preflight.

Mapping:

* returns a Model object → ``LiveProbeResult.LIVE``
* raises ``NotFound`` (HTTP 404) → ``LiveProbeResult.DEAD``
* anything else (auth, transport, region restriction) →
  ``LiveProbeResult.UNKNOWN`` so preflight downgrades to
  ``OK_PROVISIONAL`` rather than blocking the user.

Used by ``DiscoverySupport.PARTIAL`` providers in
``genblaze-google``: ``VeoProvider`` and ``ImagenProvider``. Each
provider attaches this callable to its ``ModelFamily`` instances and
forwards its lazy-built genai client via ``_invoke_family_probe``.
"""

from __future__ import annotations

import logging
from typing import Any

from genblaze_core.providers import LiveProbeResult

logger = logging.getLogger("genblaze.google.probe")


def google_models_get_probe(slug: str, *, client: Any) -> LiveProbeResult:
    """Run ``client.models.get(model=slug)`` and classify the outcome.

    Args:
        slug: Model id as the user passes it (``veo-3.0-generate-001``,
            ``imagen-3.0-generate-002``, …).
        client: Pre-built ``google.genai.Client``. The provider's lazy
            ``_get_client()`` is the supplier.
    """
    try:
        client.models.get(model=slug)
    except Exception as exc:
        status = _status_from_exception(exc)
        if status == 404:
            return LiveProbeResult.DEAD
        # 401 / 403 / 5xx / transport — caller can't conclude liveness.
        logger.debug("Google models.get probe inconclusive for %s: %s", slug, exc)
        return LiveProbeResult.UNKNOWN
    return LiveProbeResult.LIVE


def _status_from_exception(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction across google-genai error shapes.

    google-genai versions vary: some surface ``ClientError`` with a
    ``code`` attribute, older ones expose ``status_code``, and the
    underlying ``google.api_core`` exceptions carry ``code``. Fall
    through to a string scan only as a last resort.
    """
    for attr in ("status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    msg = str(exc)
    if "404" in msg or "NOT_FOUND" in msg or "not found" in msg.lower():
        return 404
    return None
