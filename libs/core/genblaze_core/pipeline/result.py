"""PipelineResult — wraps a completed pipeline run with save capabilities."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from genblaze_core.media.embedder import EmbedResult, SmartEmbedder
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step

if TYPE_CHECKING:
    from genblaze_core.models.policy import EmbedPolicy


@dataclass
class StepCompleteEvent:
    """Fired when a pipeline step finishes (success or failure).

    Attributes:
        step_index: 0-based position in the pipeline.
        total_steps: Total number of steps in the pipeline.
        step: The completed Step model.
        elapsed_sec: Wall-clock seconds since pipeline start.
    """

    step_index: int
    total_steps: int
    step: Step
    elapsed_sec: float


class PipelineResult:
    """Result of a pipeline execution, with convenience save method.

    Supports tuple unpacking for backward compatibility::

        run, manifest = pipeline.run()
    """

    def __init__(self, run: Run, manifest: Manifest) -> None:
        self.run = run
        self.manifest = manifest

    def __iter__(self) -> Iterator[Run | Manifest]:
        """Allow ``run, manifest = pipeline.run()`` destructuring."""
        yield self.run
        yield self.manifest

    def __repr__(self) -> str:
        h = self.manifest.canonical_hash[:12]
        return f"PipelineResult(run_id={self.run.run_id!r}, hash={h}...)"

    def failed_steps(self) -> list[Step]:
        """Return steps that failed during execution."""
        return [s for s in self.run.steps if s.status == StepStatus.FAILED]

    def succeeded_steps(self) -> list[Step]:
        """Return steps that succeeded during execution."""
        return [s for s in self.run.steps if s.status == StepStatus.SUCCEEDED]

    def error_summary(self) -> str | None:
        """Aggregate error messages from failed steps and transfer failures.

        Returns a multi-line string if any errors exist, or None if all steps succeeded.
        """
        lines: list[str] = []
        for i, step in enumerate(self.run.steps):
            if step.error:
                lines.append(f"Step {i} ({step.provider}/{step.model}): {step.error}")
        # Include transfer failures if the sink recorded them on the manifest
        if self.manifest.transfer_failures:
            lines.append(f"Asset transfers failed: {', '.join(self.manifest.transfer_failures)}")
        return "\n".join(lines) if lines else None

    def save(
        self,
        path: str | Path,
        *,
        embed: bool = True,
        policy: EmbedPolicy | None = None,
    ) -> EmbedResult:
        """Save the manifest, optionally embedding it into the output file.

        Args:
            path: Path to the media file to embed into.
            embed: If True, embed manifest into the file. If False, write sidecar only.
            policy: Optional embed policy for redaction.

        Returns:
            EmbedResult with paths and method used.
        """
        path = Path(path)
        embedder = SmartEmbedder()

        if not embed:
            from genblaze_core.media.sidecar import SidecarHandler

            sidecar = SidecarHandler()
            sidecar_path = sidecar.embed(path, self.manifest, policy=policy)
            return EmbedResult(
                path=path,
                sidecar_path=sidecar_path,
                manifest_uri=self.manifest.manifest_uri,
                method="sidecar",
            )

        return embedder.embed(path, self.manifest, policy=policy)
