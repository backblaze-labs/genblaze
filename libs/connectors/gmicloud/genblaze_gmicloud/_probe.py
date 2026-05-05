"""Shared family-probe primitive for GMICloud's request-queue surface.

GMICloud has no authoritative ``GET /models`` endpoint. The natural
liveness check is the **invalid-payload trick** against the
``POST /requests`` queue:

* ``POST /requests`` with ``{"model": slug, "payload": {}}`` (empty payload)
* ``404`` → the slug is gone (``LiveProbeResult.DEAD``)
* ``400`` → the slug exists; the empty payload is rejected as malformed
  (``LiveProbeResult.LIVE``)
* ``2xx`` → the queue accepted (rare for empty payloads, but treat as LIVE)
* anything else (auth / rate-limit / 5xx) → ``LiveProbeResult.UNKNOWN``

This is the canonical probe for ``DiscoverySupport.PARTIAL`` providers in
genblaze-gmicloud. Each modality provider attaches this callable to its
``ModelFamily`` instances and forwards the provider's ``httpx.Client``
via ``_invoke_family_probe``.

**Politeness note**: ``POST /requests`` does create an audit-log record
on the user's GMI account, even when the payload is rejected. The
single-flight registry cache (default 1-hour TTL) bounds the rate at
which probes fire — one probe per slug per process per hour at most.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genblaze_core.providers import LiveProbeResult

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("genblaze.gmicloud.probe")


def empty_payload_request_probe(slug: str, *, http: httpx.Client) -> LiveProbeResult:
    """Run the invalid-payload trick against ``POST /requests``.

    Implementations of :class:`~genblaze_core.providers.FamilyProbe`
    receive the provider's already-configured ``httpx.Client`` (auth
    header pre-set). We POST a minimal envelope naming the slug and an
    empty payload.

    On the rare 2xx outcome (GMI accepted the empty payload), we issue
    a best-effort ``DELETE /requests/{id}`` to avoid leaving a phantom
    job in the user's queue. The cancel is best-effort; the probe
    verdict is ``LIVE`` either way because the model is plainly callable.
    """
    try:
        resp = http.post("/requests", json={"model": slug, "payload": {}})
    except Exception as exc:
        logger.debug("GMICloud empty-payload probe transport error for %s: %s", slug, exc)
        return LiveProbeResult.UNKNOWN

    status = resp.status_code
    if status == 404:
        return LiveProbeResult.DEAD
    if status == 400:
        return LiveProbeResult.LIVE
    if 200 <= status < 300:
        # Surprise — empty payload accepted. Cancel the phantom job so
        # we don't pollute the user's queue. Best-effort: any failure
        # here is logged at DEBUG and ignored — the probe verdict
        # is LIVE regardless of whether cancel succeeded.
        try:
            body = resp.json()
            request_id = body.get("request_id") or body.get("id")
            if request_id:
                http.delete(f"/requests/{request_id}")
        except (ValueError, Exception) as exc:
            logger.debug(
                "GMICloud empty-payload probe cancel-on-2xx failed for %s: %s",
                slug,
                exc,
            )
        return LiveProbeResult.LIVE
    # 401/403/429/5xx — informative but inconclusive on slug liveness.
    return LiveProbeResult.UNKNOWN
