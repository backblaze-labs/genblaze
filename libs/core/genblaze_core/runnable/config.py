"""Runnable configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TypeAlias

    from genblaze_core.observability.events import StepRetriedEvent
    from genblaze_core.providers.progress import ProgressEvent

    # Callback shapes shared by RunnableConfig and the Pipeline entry points.
    # Typing-only: the callables are plain functions at runtime.
    ProgressCallback: TypeAlias = Callable[[ProgressEvent], None]
    RetryCallback: TypeAlias = Callable[[StepRetriedEvent], None]


class RunnableConfig(TypedDict, total=False):
    """Configuration passed through the runnable chain."""

    tags: list[str]
    metadata: dict[str, Any]
    run_id: str
    # Tenancy is set via Pipeline(tenant_id=...), not per-call config (see #68).
    timeout: float
    max_retries: int
    on_progress: ProgressCallback | None
    # Fired after submit() with (step_id, prediction_id) for checkpoint persistence
    on_submit: Callable[[str, Any], None] | None
    # Fired before each retry sleep; lets the pipeline stream ``step.retried`` events
    on_retry: RetryCallback | None
