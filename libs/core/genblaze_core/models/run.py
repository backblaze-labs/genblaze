"""Run model — a collection of generation steps."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from genblaze_core._utils import new_id, utc_now
from genblaze_core.models.enums import RunStatus
from genblaze_core.models.step import Step


class Run(BaseModel):
    """A collection of generation steps forming a pipeline execution."""

    run_id: str = Field(default_factory=new_id, description="Unique run identifier (UUID).")
    tenant_id: str | None = Field(default=None, description="Tenant identifier for multi-tenancy.")
    project_id: str | None = Field(default=None, description="Project identifier.")
    name: str | None = Field(default=None, description="Human-readable run name.")
    status: RunStatus = Field(default=RunStatus.PENDING, description="Current run status.")
    steps: list[Step] = Field(
        default_factory=list, description="Ordered list of generation steps."
    )
    parent_run_id: str | None = Field(
        default=None, description="Parent run ID for replay/fork lineage."
    )
    idempotency_key: str | None = Field(default=None, description="Client-provided key for dedup.")
    created_at: datetime = Field(default_factory=utc_now, description="Run creation timestamp.")
    started_at: datetime | None = Field(default=None, description="Execution start timestamp.")
    completed_at: datetime | None = Field(
        default=None, description="Execution completion timestamp."
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata.")

    def __repr__(self) -> str:
        return (
            f"Run(id={self.run_id[:8]}..., name={self.name!r}, "
            f"steps={len(self.steps)}, status={self.status})"
        )
