"""Base sink ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod

from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step


class BaseSink(ABC):
    """Abstract base for event/manifest sinks."""

    @abstractmethod
    def write_run(self, run: Run, manifest: Manifest) -> None:
        """Persist a completed run and its manifest."""
        ...

    def on_step_complete(  # noqa: B027 — intentionally empty default; sinks opt in.
        self,
        step: Step,
        *,
        run_id: str,
        tenant_id: str | None,
        date_str: str,
    ) -> None:
        """Pipeline hook — called after each step finishes execution.

        Default is a no-op. Sinks that support eager asset transfer
        (overlapping upload with subsequent step generation) override
        this to submit transfers to a background pool. The pipeline
        calls ``write_run`` at the end regardless; eager sinks simply
        have less work to do in that final call.

        Called for every completed step, including failed ones — sinks
        decide what (if anything) to do based on ``step.status``.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any held resources."""
        ...
