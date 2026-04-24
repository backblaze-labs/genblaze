"""Stream events — push-style notifications emitted during pipeline execution.

Events are emitted by :meth:`Pipeline.stream` / :meth:`Pipeline.astream` and
also forwarded to every registered :class:`Tracer`. Consumers iterate over
events to render progress UI, power real-time dashboards, or feed agent loops.

Shape: a discriminated union keyed on ``type``. :class:`StreamEvent` is the
shared base; each event variant is a Pydantic subclass that declares *only*
the fields that variant carries as required. Consumers can narrow via
``isinstance(ev, StepFailedEvent)`` or ``ev.type == "step.failed"`` — both
produce precise types under pyright/mypy.

In-process objects (``step``, ``result``) live on the model for Python
callers but are excluded from JSON serialization. Derived, JSON-safe fields
(``step_status``, ``manifest_hash``, ``run_status``) are populated at
construction time so consumers reading the wire format don't lose context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from genblaze_core._utils import utc_now
from genblaze_core.models.step import Step
from genblaze_core.pipeline.result import PipelineResult

# Tagged-union event types. Keep these string literals stable — external
# consumers (dashboards, tracers, TS clients) match on them.
StreamEventType = Literal[
    "pipeline.started",
    "pipeline.completed",
    "pipeline.failed",
    "step.started",
    "step.progress",
    "step.retried",
    "step.completed",
    "step.failed",
    "agent.iteration.started",
    "agent.iteration.evaluated",
    "agent.completed",
]


class StreamEvent(BaseModel):
    """Base class for every pipeline/agent streaming event.

    Holds the fields universal to all variants: ``type`` and ``timestamp``.
    Variant-specific fields live on subclasses. ``isinstance(x, StreamEvent)``
    remains truthy for any variant so runtime type checks in the pipeline
    plumbing keep working after the discriminated-union migration.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    type: StreamEventType = Field(description="Discriminator tag identifying the event variant.")
    timestamp: datetime = Field(
        default_factory=utc_now,
        description="When this event was created (UTC).",
    )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict — drops in-process objects, preserves derived fields.

        Fields excluded from serialization (``step``, ``result``) are provided
        for in-process consumers only; their JSON-surface counterparts
        (``step_status``, ``manifest_hash``, ``run_status``, ``error``) are
        populated at construction and do appear here.
        """
        return self.model_dump(mode="json", exclude_none=True)


# --- Pipeline-level events --------------------------------------------------


class PipelineStartedEvent(StreamEvent):
    """Emitted once at the start of a pipeline run."""

    type: Literal["pipeline.started"] = "pipeline.started"
    run_id: str = Field(description="Run identifier this event belongs to.")
    total_steps: int = Field(description="Total number of steps in the pipeline.", ge=0)
    message: str | None = Field(default=None, description="Pipeline name, if named.")


class PipelineCompletedEvent(StreamEvent):
    """Emitted once at the end of a successful pipeline run."""

    type: Literal["pipeline.completed"] = "pipeline.completed"
    run_id: str = Field(description="Run identifier this event belongs to.")
    run_status: str | None = Field(
        default=None, description="Terminal run status (e.g. ``completed``)."
    )
    manifest_hash: str | None = Field(
        default=None, description="Canonical SHA-256 hash of the run's manifest."
    )
    # In-process only — full :class:`PipelineResult`. Excluded from JSON
    # serialization; wire consumers read ``manifest_hash`` / ``run_status``.
    result: PipelineResult | None = Field(default=None, exclude=True, repr=False)


class PipelineFailedEvent(StreamEvent):
    """Emitted once when a pipeline run terminates in failure."""

    type: Literal["pipeline.failed"] = "pipeline.failed"
    run_id: str = Field(description="Run identifier this event belongs to.")
    message: str | None = Field(default=None, description="Short human-readable failure summary.")
    run_status: str | None = Field(
        default=None, description="Terminal run status (``failed`` or similar)."
    )
    manifest_hash: str | None = Field(
        default=None, description="Canonical hash of the partial manifest, if one was produced."
    )
    # In-process only — excluded from JSON serialization.
    result: PipelineResult | None = Field(default=None, exclude=True, repr=False)


# --- Step-level events ------------------------------------------------------


class StepStartedEvent(StreamEvent):
    """Emitted when a step transitions from queued to running."""

    type: Literal["step.started"] = "step.started"
    run_id: str = Field(description="Run identifier this event belongs to.")
    step_id: str = Field(description="Step identifier (UUID).")
    step_index: int = Field(description="0-based step position in the pipeline.", ge=0)
    total_steps: int = Field(description="Total number of steps in the pipeline.", ge=0)
    provider: str = Field(description="Provider name (e.g. ``gmicloud``).")
    model: str = Field(description="Model slug passed to the provider.")


class StepProgressEvent(StreamEvent):
    """Emitted on provider progress ticks (may fire many times per step)."""

    type: Literal["step.progress"] = "step.progress"
    run_id: str | None = Field(default=None, description="Run identifier if available.")
    step_id: str = Field(description="Step identifier (UUID).")
    provider: str = Field(description="Provider name.")
    model: str = Field(description="Model slug.")
    progress_pct: float | None = Field(
        default=None,
        description="Progress ratio in [0.0, 1.0], if the provider reports one.",
        ge=0.0,
        le=1.0,
    )
    elapsed_sec: float | None = Field(
        default=None, description="Wall-clock seconds since step submission.", ge=0
    )
    preview_url: str | None = Field(
        default=None, description="Ephemeral preview URL, if the provider emits one."
    )
    message: str | None = Field(default=None, description="Optional provider-supplied note.")
    data: dict[str, Any] = Field(
        default_factory=dict, description="Provider-specific extra fields (e.g. polling status)."
    )


class StepRetriedEvent(StreamEvent):
    """Emitted when a transient phase failure triggers a retry.

    Fires once per retry attempt (not per final failure). ``phase`` lets UIs
    distinguish an expensive submit retry from a cheap poll retry; ``delay_sec``
    is the actual sleep scheduled before the next attempt (post-jitter,
    post-``Retry-After``-clamp).
    """

    type: Literal["step.retried"] = "step.retried"
    run_id: str | None = Field(default=None, description="Run identifier if available.")
    step_id: str = Field(description="Step identifier (UUID).")
    provider: str = Field(description="Provider name.")
    model: str = Field(description="Model slug.")
    phase: Literal["submit", "poll", "fetch"] = Field(
        description="Which lifecycle phase is being retried."
    )
    attempt: int = Field(description="1-based attempt counter that just failed.", ge=1)
    max_attempts: int = Field(description="Total attempts permitted for this phase.", ge=1)
    delay_sec: float = Field(
        description="Seconds the retry helper will sleep before the next attempt.", ge=0
    )
    error_code: str | None = Field(
        default=None, description="Normalized ProviderErrorCode that triggered the retry."
    )
    error: str | None = Field(default=None, description="Sanitized failure message, if available.")


class StepCompletedEvent(StreamEvent):
    """Emitted when a step finishes successfully."""

    type: Literal["step.completed"] = "step.completed"
    run_id: str | None = Field(default=None, description="Run identifier if available.")
    step_id: str = Field(description="Step identifier (UUID).")
    step_index: int = Field(description="0-based step position in the pipeline.", ge=0)
    total_steps: int = Field(description="Total number of steps in the pipeline.", ge=0)
    provider: str = Field(description="Provider name.")
    model: str = Field(description="Model slug.")
    elapsed_sec: float = Field(
        description="Wall-clock seconds from step start to completion.", ge=0
    )
    step_status: str | None = Field(
        default=None, description="Terminal status string (usually ``succeeded``)."
    )
    # In-process only — full :class:`Step` with assets, cost, etc.
    # Excluded from JSON serialization (wire consumers read the derived
    # ``step_status`` field).
    step: Step | None = Field(default=None, exclude=True, repr=False)


class StepFailedEvent(StreamEvent):
    """Emitted when a step terminates in failure."""

    type: Literal["step.failed"] = "step.failed"
    run_id: str | None = Field(default=None, description="Run identifier if available.")
    step_id: str = Field(description="Step identifier (UUID).")
    step_index: int = Field(description="0-based step position in the pipeline.", ge=0)
    total_steps: int = Field(description="Total number of steps in the pipeline.", ge=0)
    provider: str = Field(description="Provider name.")
    model: str = Field(description="Model slug.")
    elapsed_sec: float = Field(description="Wall-clock seconds from step start to failure.", ge=0)
    error: str | None = Field(default=None, description="Failure reason surfaced from the step.")
    step_status: str | None = Field(
        default=None, description="Terminal status string (usually ``failed``)."
    )
    # In-process only — excluded from JSON serialization.
    step: Step | None = Field(default=None, exclude=True, repr=False)


# --- Agent-loop events ------------------------------------------------------


class AgentIterationStartedEvent(StreamEvent):
    """Emitted at the start of each agent-loop iteration."""

    type: Literal["agent.iteration.started"] = "agent.iteration.started"
    iteration: int = Field(description="0-based iteration counter.", ge=0)
    total: int = Field(description="Maximum iterations configured for this loop.", ge=1)
    message: str | None = Field(
        default=None, description="Feedback from the previous iteration, if any."
    )


class AgentIterationEvaluatedEvent(StreamEvent):
    """Emitted after an iteration's result has been evaluated."""

    type: Literal["agent.iteration.evaluated"] = "agent.iteration.evaluated"
    iteration: int = Field(description="0-based iteration counter this evaluation covers.", ge=0)
    passed: bool = Field(description="Whether the evaluation's pass criterion was met.")
    score: float | None = Field(
        default=None,
        description="Evaluator score (usually in [0, 1] — evaluator-defined). "
        "May be absent when the evaluator only returns pass/fail.",
    )
    feedback: str | None = Field(default=None, description="Free-text evaluator feedback.")
    # In-process only — excluded from JSON serialization.
    result: PipelineResult | None = Field(default=None, exclude=True, repr=False)


class AgentCompletedEvent(StreamEvent):
    """Emitted once when the agent loop terminates (pass, fail, or max-iters)."""

    type: Literal["agent.completed"] = "agent.completed"
    passed: bool = Field(description="Whether the final iteration's evaluation passed.")
    iterations: int = Field(description="Total number of iterations executed.", ge=0)
    total_cost_usd: float | None = Field(
        default=None, description="Summed cost across all iterations, if tracked."
    )
    # In-process only — excluded from JSON serialization.
    result: PipelineResult | None = Field(default=None, exclude=True, repr=False)


# --- Discriminated union surface -------------------------------------------


#: Discriminated union of every concrete event variant. Use this type when
#: parsing events from external input (e.g. SSE streams, webhook bodies) —
#: Pydantic routes by the ``type`` discriminator and returns the correct
#: variant class. For type annotations that accept any event, prefer the
#: base class :class:`StreamEvent` — it's more ergonomic and still narrows
#: via ``isinstance``.
AnyStreamEvent = Annotated[
    PipelineStartedEvent
    | PipelineCompletedEvent
    | PipelineFailedEvent
    | StepStartedEvent
    | StepProgressEvent
    | StepRetriedEvent
    | StepCompletedEvent
    | StepFailedEvent
    | AgentIterationStartedEvent
    | AgentIterationEvaluatedEvent
    | AgentCompletedEvent,
    Field(discriminator="type"),
]

#: Parser for inbound event dicts. ``StreamEventAdapter.validate_python(data)``
#: returns the correctly-typed variant based on ``data["type"]``.
StreamEventAdapter: TypeAdapter[AnyStreamEvent] = TypeAdapter(AnyStreamEvent)


__all__ = [
    "AgentCompletedEvent",
    "AgentIterationEvaluatedEvent",
    "AgentIterationStartedEvent",
    "AnyStreamEvent",
    "PipelineCompletedEvent",
    "PipelineFailedEvent",
    "PipelineStartedEvent",
    "StepCompletedEvent",
    "StepFailedEvent",
    "StepProgressEvent",
    "StepRetriedEvent",
    "StepStartedEvent",
    "StreamEvent",
    "StreamEventAdapter",
    "StreamEventType",
]
