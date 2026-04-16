"""Stream events — push-style notifications emitted during pipeline execution.

Events are emitted by `Pipeline.stream()` / `Pipeline.astream()` and also
forwarded to any registered `Tracer`. Consumers iterate over events to
render progress UI, power real-time dashboards, or feed agent loops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from genblaze_core._utils import utc_now

if TYPE_CHECKING:
    from genblaze_core.models.step import Step
    from genblaze_core.pipeline.result import PipelineResult


# Tagged-union event types. Keep these string literals stable — external
# consumers (dashboards, tracers) match on them.
StreamEventType = Literal[
    "pipeline.started",
    "pipeline.completed",
    "pipeline.failed",
    "step.started",
    "step.progress",
    "step.completed",
    "step.failed",
    "agent.iteration.started",
    "agent.iteration.evaluated",
    "agent.completed",
]


@dataclass
class StreamEvent:
    """Event emitted during pipeline or agent-loop execution.

    Fields are a superset — only fields relevant to the event type are populated.
    Consumers should branch on `type`.
    """

    type: StreamEventType
    run_id: str | None = None
    step_id: str | None = None
    step_index: int | None = None
    total_steps: int | None = None
    provider: str | None = None
    model: str | None = None
    progress_pct: float | None = None
    preview_url: str | None = None
    message: str | None = None
    elapsed_sec: float | None = None
    # `step` populated on step.completed / step.failed
    step: Step | None = None
    # `result` populated on pipeline.completed / pipeline.failed / agent.completed
    result: PipelineResult | None = None
    # Arbitrary extra payload — e.g. evaluator feedback, agent iteration index
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (drops model objects, keeps run_id refs)."""
        d: dict[str, Any] = {
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
        }
        for k in (
            "run_id",
            "step_id",
            "step_index",
            "total_steps",
            "provider",
            "model",
            "progress_pct",
            "preview_url",
            "message",
            "elapsed_sec",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.step is not None:
            d["step_status"] = str(self.step.status)
            if self.step.error:
                d["error"] = self.step.error
        if self.result is not None:
            d["run_status"] = str(self.result.run.status)
            d["manifest_hash"] = self.result.manifest.canonical_hash
        if self.data:
            d["data"] = self.data
        return d
