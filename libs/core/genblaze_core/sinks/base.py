"""Base sink ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod

from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run


class BaseSink(ABC):
    """Abstract base for event/manifest sinks."""

    @abstractmethod
    def write_run(self, run: Run, manifest: Manifest) -> None:
        """Persist a completed run and its manifest."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release any held resources."""
        ...
