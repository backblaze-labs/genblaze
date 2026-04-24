"""Runnable configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable

    from genblaze_core.observability.events import StepRetriedEvent
    from genblaze_core.providers.progress import ProgressEvent


class RunnableConfig(TypedDict, total=False):
    """Configuration passed through the runnable chain."""

    tags: list[str]
    metadata: dict[str, Any]
    run_id: str
    tenant_id: str
    timeout: float
    max_retries: int
    on_progress: Callable[[ProgressEvent], None] | None
    # Fired after submit() with (step_id, prediction_id) for checkpoint persistence
    on_submit: Callable[[str, Any], None] | None
    # Fired before each retry sleep; lets the pipeline stream ``step.retried`` events
    on_retry: Callable[[StepRetriedEvent], None] | None
