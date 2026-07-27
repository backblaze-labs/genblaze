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


def google_imagen_predict_probe(slug: str, *, client: Any) -> LiveProbeResult:
    """Imagen-only probe: ``models.get`` catalog membership is not entitlement.

    On a newly created Gemini API key, ``client.models.get(model=slug)``
    returns 200 for every ``imagen-4.0-*`` slug (it's in the catalog), but
    the ``:predict`` call ``ImagenProvider`` actually makes 404s "no longer
    available to new users" — an account-entitlement gate that sits in
    front of ``:predict``, not a catalog-membership question. Trusting
    ``models.get`` alone reports these slugs LIVE at preflight and lets the
    pipeline fail mid-run instead (issue #206).

    This wraps ``google_models_get_probe`` with one extra, deliberately
    invalid ``generate_images`` call — the same "empty/invalid payload"
    trick ``genblaze_nvidia``/``genblaze_gmicloud`` use for their own
    catalog-less probes, adapted to the ``google-genai`` SDK surface.
    Verified against the real SDK path (not just a raw-HTTP repro):
    ``google.genai.types._GenerateImagesParameters`` performs no local
    validation on an empty ``prompt``, so ``generate_images(prompt="")``
    always reaches the wire instead of short-circuiting client-side —
    which would otherwise make this probe always report LIVE regardless
    of entitlement.

    Classification, once ``models.get`` already returned LIVE:

    * probe call raises with a 404 status → ``DEAD``. We already know the
      slug exists (models.get said so), so a 404 here can only be the
      entitlement gate, not a missing model.
    * probe call raises with anything else (400 "bad request" from our
      deliberately empty prompt, 401/403/5xx, transport error) → the
      catalog-membership ``LIVE`` stands. A single inconclusive or
      validation-shaped failure on our own malformed probe request isn't
      evidence the slug is unusable.
    * probe call succeeds outright → ``LIVE`` (strongest possible signal).

    Only wired onto ``GOOGLE_IMAGEN_FAMILY`` — Veo and the Gemini-native
    image family have no reported entitlement gap, so they keep the
    plain, single-call ``google_models_get_probe``.

    Adds one extra call per non-cached preflight check; bounded by the
    provider's existing probe cache (``probe_cache_ttl`` /
    ``probe_cache_max_entries`` constructor kwargs) same as any other
    probe result.
    """
    result = google_models_get_probe(slug, client=client)
    if result is not LiveProbeResult.LIVE:
        return result  # DEAD/UNKNOWN from models.get is already conclusive.

    try:
        client.models.generate_images(model=slug, prompt="")
    except Exception as exc:
        if _status_from_exception(exc) == 404:
            logger.debug(
                "Google imagen predict-probe: %s is catalog-listed but "
                "entitlement-gated (404 on :predict)",
                slug,
            )
            return LiveProbeResult.DEAD
        # Any other outcome (400 from our own malformed request, auth,
        # rate-limit, transport) isn't evidence against the models.get LIVE.
        logger.debug("Google imagen predict-probe inconclusive for %s: %s", slug, exc)
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
