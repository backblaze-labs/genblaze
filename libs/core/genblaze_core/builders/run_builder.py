"""Fluent builder for Run models."""

from __future__ import annotations

from genblaze_core.models.enums import RunStatus
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step


class RunBuilder:
    """Fluent builder for Run models.

    Use ``RunBuilder(name)`` to start a chain (name is optional), then call
    methods (all returning ``self`` for chaining). Add steps via
    ``add_step()`` (which accepts a ``Step`` built by ``StepBuilder``), then
    call ``.build()`` to produce a ``Run`` instance.
    """

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
        """Set the tenant identifier for multi-tenancy."""
        self._data["tenant_id"] = tenant_id
        return self

    def project(self, project_id: str) -> RunBuilder:
        """Set the project identifier."""
        self._data["project_id"] = project_id
        return self

    def parent(self, parent_run_id: str) -> RunBuilder:
        """Link this run to a parent for iteration/fork lineage."""
        self._data["parent_run_id"] = parent_run_id
        return self

    def status(self, s: RunStatus) -> RunBuilder:
        """Set the initial run status."""
        self._data["status"] = s
        return self

    def add_step(self, step: Step) -> RunBuilder:
        """Append a ``Step`` to this run.

        The step's ``run_id`` and ``step_index`` are set by ``.build()``.
        """
        self._steps.append(step)
        return self

    def meta(self, **kwargs) -> RunBuilder:
        """Add arbitrary metadata as key-value pairs."""
        self._data.setdefault("metadata", {}).update(kwargs)
        return self

    def build(self) -> Run:
        """Build and return a ``Run`` instance with steps wired up."""
        run = Run(**self._data)
        # Copy steps to avoid mutating caller-owned objects (safe for double-build)
        steps = [s.model_copy() for s in self._steps]
        for idx, step in enumerate(steps):
            step.run_id = run.run_id
            step.step_index = idx
        run.steps = steps
        return run
