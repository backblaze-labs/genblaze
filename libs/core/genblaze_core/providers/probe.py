"""Model-probe contract — cheap liveness checks for registered model ids.

Connectors implement ``BaseProvider.probe_model(model_id)`` and return a
``ProbeResult``. The CI script ``tools/probe_models.py`` walks every
entry-point-registered provider, calls ``probe_model`` for every default id
exposed by ``models_default()``, and fails the workflow if any default is
``NOT_FOUND``. This is the credibility gate that prevents shipped registries
from drifting away from the live API.

Two probe strategies, both legal:

* **Catalog endpoint** — providers with a cheap ``GET /models`` endpoint
  (OpenAI, Replicate, Google) intersect their registered ids against the
  live catalog. No tokens spent, no records created.
* **Invalid-payload trick** — request-queue providers (GMI, Runway, Luma)
  POST a deliberately-empty payload. ``404`` ⇒ ``NOT_FOUND``. ``400`` ⇒ the
  model exists but the payload is bad ⇒ ``OK``. Be polite — these create
  records in the upstream's audit log.

Default impl is ``ProbeResult.SKIPPED``. A connector that can't (or won't)
probe stays opted out; the CI report flags ``SKIPPED`` rows so we know
which surfaces aren't being checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProbeStatus(StrEnum):
    """Outcome of a single ``BaseProvider.probe_model(model_id)`` call."""

    OK = "ok"
    NOT_FOUND = "not_found"
    AUTH = "auth"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Structured outcome plus optional context for the CI report.

    ``detail`` carries a short human-readable hint (e.g. the upstream error
    text) so the failing-row entry in ``model-probe-status.json`` is
    debuggable without re-running the probe.
    """

    status: ProbeStatus
    detail: str | None = field(default=None)

    @classmethod
    def ok(cls, detail: str | None = None) -> ProbeResult:
        return cls(status=ProbeStatus.OK, detail=detail)

    @classmethod
    def not_found(cls, detail: str | None = None) -> ProbeResult:
        return cls(status=ProbeStatus.NOT_FOUND, detail=detail)

    @classmethod
    def auth(cls, detail: str | None = None) -> ProbeResult:
        return cls(status=ProbeStatus.AUTH, detail=detail)

    @classmethod
    def skipped(cls, detail: str | None = None) -> ProbeResult:
        return cls(status=ProbeStatus.SKIPPED, detail=detail)

    @classmethod
    def unknown(cls, detail: str | None = None) -> ProbeResult:
        return cls(status=ProbeStatus.UNKNOWN, detail=detail)

    @property
    def is_failure(self) -> bool:
        """True if the probe definitively says the model is missing.

        Used by ``tools/probe_models.py`` to decide CI exit code: only
        ``NOT_FOUND`` fails the build. ``AUTH`` / ``UNKNOWN`` / ``SKIPPED``
        are reported but non-blocking.
        """
        return self.status is ProbeStatus.NOT_FOUND
