"""Progress events for provider poll loops."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProgressEvent:
    """Fired during provider poll loops to report generation progress.

    Attributes:
        step_id: ID of the step being executed.
        provider: Provider name (e.g. "runway", "luma").
        model: Model identifier.
        status: Current status — "submitted", "processing", "succeeded", "failed".
        progress_pct: 0.0–1.0 if the provider reports progress, else None.
        elapsed_sec: Seconds elapsed since step start.
        message: Optional human-readable status message.
    """

    step_id: str
    provider: str
    model: str
    status: str
    progress_pct: float | None
    elapsed_sec: float
    message: str | None = None
