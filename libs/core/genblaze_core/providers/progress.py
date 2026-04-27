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
        preview_url: Optional URL to an intermediate preview (e.g. draft frame,
            waveform thumbnail). Providers set this opportunistically when the
            underlying API exposes in-progress artifacts.
        request_id: Upstream provider's prediction/job id, available once submit
            returns. Lets dashboards show debug info (e.g. a "view in
            Replicate" link) live, instead of waiting for step completion.
    """

    step_id: str
    provider: str
    model: str
    status: str
    progress_pct: float | None
    elapsed_sec: float
    message: str | None = None
    preview_url: str | None = None
    request_id: str | None = None
