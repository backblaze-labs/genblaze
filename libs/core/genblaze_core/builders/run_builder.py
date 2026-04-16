"""Fluent builder for Run models."""

from __future__ import annotations

from genblaze_core.models.enums import RunStatus
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step


class RunBuilder:
    def __init__(self, name: str | None = None):
        self._data: dict = {}
        if name:
            self._data["name"] = name
        self._steps: list[Step] = []

    def run_id(self, id: str) -> RunBuilder:
        """Set an explicit run ID (useful for correlation)."""
        self._data["run_id"] = id
        return self

    def tenant(self, tenant_id: str) -> RunBuilder:
        self._data["tenant_id"] = tenant_id
        return self

    def project(self, project_id: str) -> RunBuilder:
        self._data["project_id"] = project_id
        return self

    def parent(self, parent_run_id: str) -> RunBuilder:
        """Link this run to a parent for iteration/fork lineage."""
        self._data["parent_run_id"] = parent_run_id
        return self

    def status(self, s: RunStatus) -> RunBuilder:
        self._data["status"] = s
        return self

    def add_step(self, step: Step) -> RunBuilder:
        self._steps.append(step)
        return self

    def meta(self, **kwargs) -> RunBuilder:
        self._data.setdefault("metadata", {}).update(kwargs)
        return self

    def build(self) -> Run:
        run = Run(**self._data)
        # Copy steps to avoid mutating caller-owned objects (safe for double-build)
        steps = [s.model_copy() for s in self._steps]
        for idx, step in enumerate(steps):
            step.run_id = run.run_id
            step.step_index = idx
        run.steps = steps
        return run
