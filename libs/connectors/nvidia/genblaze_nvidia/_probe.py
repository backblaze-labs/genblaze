"""Shared family-probe primitive for NVIDIA generative endpoints.

NVIDIA's ``ai.api.nvidia.com/v1/genai/{vendor}/{slug}`` surface has no
authoritative ``GET /models`` endpoint — chat lives on a different host
(``integrate.api.nvidia.com``) and that catalog excludes the generative
slugs entirely. The natural authoritative-liveness check is the
**invalid-payload trick**:

* ``POST /genai/{slug}`` with ``{}`` (empty body)
* ``404`` → the slug is gone (``LiveProbeResult.DEAD``)
* ``400`` → the slug exists; we just sent garbage
  (``LiveProbeResult.LIVE``)
* ``2xx`` → the endpoint accepted the empty body
  (``LiveProbeResult.LIVE``)
* anything else → ``LiveProbeResult.UNKNOWN``

This is the canonical probe for ``DiscoverySupport.PARTIAL`` providers in
genblaze. Each NVIDIA generative provider (audio / video / image)
attaches this callable to its ``ModelFamily`` instances and forwards the
provider's ``httpx.Client`` via ``_invoke_family_probe``.

The probe is **polite** — empty-payload POSTs do not enqueue real jobs
on NIM; they're rejected as malformed before the model runs. No tokens
spent, no records created on the user's audit log.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genblaze_core.providers import LiveProbeResult

from ._base import build_generation_path

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("genblaze.nvidia.probe")


def empty_payload_genai_probe(slug: str, *, http: httpx.Client) -> LiveProbeResult:
    """Run the invalid-payload trick against ``/genai/{slug}``.

    Implementations of :class:`~genblaze_core.providers.FamilyProbe`
    receive an ``httpx.Client`` configured for the generation base URL
    (``ai.api.nvidia.com/v1``); we POST against the model-specific path
    derived from ``slug``.
    """
    path = build_generation_path(slug)
    try:
        resp = http.post(path, json={})
    except Exception as exc:
        # Transport-level failure (DNS, TLS, timeout) — caller decides.
        # Logged at DEBUG: this is expected during offline tests.
        logger.debug("NVIDIA empty-payload probe transport error for %s: %s", slug, exc)
        return LiveProbeResult.UNKNOWN

    status = resp.status_code
    if status == 404:
        return LiveProbeResult.DEAD
    if status == 400 or 200 <= status < 300:
        return LiveProbeResult.LIVE
    # 401/403/429/5xx — informative but inconclusive on slug liveness.
    return LiveProbeResult.UNKNOWN
