"""LangSmith tracer — maps genblaze run/step lifecycle to LangSmith runs.

Each pipeline run becomes a ``chain`` run in LangSmith; each step becomes
a nested child run tagged with ``genblaze.step``. Progress ticks are
attached as events on the step run.

The LangSmith SDK is imported lazily in ``__init__`` so this module can be
loaded in test environments without the SDK installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from genblaze_core.observability.tracer import Tracer

if TYPE_CHECKING:
    from genblaze_core.models.step import Step
    from genblaze_core.observability.events import StreamEvent
    from genblaze_core.pipeline.result import PipelineResult

logger = logging.getLogger("genblaze.langsmith")


class LangSmithTracer(Tracer):
    """Route pipeline observability to LangSmith.

    Args:
        project_name: Target LangSmith project (defaults to ``default``).
        api_key: Optional LangSmith API key (falls back to env vars).
        client: Pre-configured ``langsmith.Client``. If omitted, one is created.
    """

    def __init__(
        self,
        *,
        project_name: str = "default",
        api_key: str | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            try:
                from langsmith import Client
            except ImportError as exc:  # pragma: no cover — import guard
                raise ImportError(
                    "genblaze-langsmith requires the 'langsmith' package. "
                    "Install with: pip install genblaze-langsmith"
                ) from exc
            client = Client(api_key=api_key) if api_key else Client()
        self._client = client
        self._project = project_name
        # Track in-flight LangSmith run UUIDs so step runs can parent correctly.
        self._run_ids: dict[str, str] = {}  # pipeline run_id → LangSmith run id
        self._step_run_ids: dict[str, str] = {}  # step_id → LangSmith run id

    def on_run_start(
        self,
        run_id: str,
        name: str | None,
        *,
        tenant_id: str | None = None,
        total_steps: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            from uuid import uuid4

            ls_run_id = str(uuid4())
            self._run_ids[run_id] = ls_run_id
            self._client.create_run(
                id=ls_run_id,
                name=name or "genblaze-pipeline",
                run_type="chain",
                inputs={"total_steps": total_steps, "tenant_id": tenant_id},
                project_name=self._project,
                tags=["genblaze", "pipeline"],
                extra={"metadata": {"genblaze.run_id": run_id, **(metadata or {})}},
            )
        except Exception as exc:  # noqa: BLE001 — observability must not break pipelines
            logger.warning("LangSmith on_run_start failed: %s", exc)

    def on_step_start(
        self,
        run_id: str,
        step: Step,
        *,
        step_index: int,
        total_steps: int,
    ) -> None:
        try:
            from uuid import uuid4

            parent = self._run_ids.get(run_id)
            ls_step_id = str(uuid4())
            self._step_run_ids[step.step_id] = ls_step_id
            self._client.create_run(
                id=ls_step_id,
                name=f"{step.provider}/{step.model}",
                run_type="llm",
                inputs={
                    "prompt": step.prompt,
                    "params": step.params,
                    "modality": str(step.modality),
                },
                parent_run_id=parent,
                project_name=self._project,
                tags=["genblaze", "step", step.provider],
                extra={
                    "metadata": {
                        "genblaze.step_id": step.step_id,
                        "genblaze.step_index": step_index,
                        "genblaze.total_steps": total_steps,
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangSmith on_step_start failed: %s", exc)

    def on_event(self, event: StreamEvent) -> None:
        # Progress ticks are noisy; skip them by default. Callers wanting
        # per-tick visibility can wrap this tracer in a CompositeTracer with
        # a LoggingTracer.
        return

    def on_step_end(
        self,
        run_id: str,
        step: Step,
        *,
        duration_ms: float,
        step_index: int,
    ) -> None:
        ls_id = self._step_run_ids.pop(step.step_id, None)
        if ls_id is None:
            return
        try:
            outputs = {
                "assets": [{"url": a.url, "media_type": a.media_type} for a in step.assets],
                "status": str(step.status),
                "duration_ms": duration_ms,
            }
            if step.cost_usd is not None:
                outputs["cost_usd"] = step.cost_usd
            self._client.update_run(
                ls_id,
                outputs=outputs,
                error=step.error,
                end_time=None,  # SDK sets server-side
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangSmith on_step_end failed: %s", exc)

    def on_run_end(self, run_id: str, result: PipelineResult) -> None:
        ls_id = self._run_ids.pop(run_id, None)
        if ls_id is None:
            return
        try:
            self._client.update_run(
                ls_id,
                outputs={
                    "status": str(result.run.status),
                    "manifest_hash": result.manifest.canonical_hash,
                    "n_steps": len(result.run.steps),
                },
                error=result.error_summary(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangSmith on_run_end failed: %s", exc)
