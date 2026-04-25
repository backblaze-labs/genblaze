"""Genblaze exception hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Note: we *cannot* import ``RunStatus`` / ``StepStatus`` at module top —
# although ``genblaze_core.models.enums`` itself is a leaf, ``from … import …``
# triggers the parent package's ``__init__.py`` which eagerly loads
# ``manifest.py``, which imports ``ManifestError`` back from this module.
# The cycle only manifests when ``exceptions.py`` is the first import in a
# fresh process (e.g. the S3 connector tests). Use the module-level
# ``_enum_status`` helper below for a sys.modules-cached lookup with no
# circular-import risk.

if TYPE_CHECKING:
    from genblaze_core.models.enums import ProviderErrorCode, RunStatus, StepStatus
    from genblaze_core.pipeline.result import PipelineResult


def _enum_status() -> tuple[type[RunStatus], type[StepStatus]]:
    """Lazily resolve ``RunStatus`` / ``StepStatus`` once per process.

    First call pays the import cost; every subsequent call is a ``sys.modules``
    dict lookup. Avoids the circular import that ``from ... import`` at module
    top would cause via ``genblaze_core/models/__init__.py``.
    """
    from genblaze_core.models import enums

    return enums.RunStatus, enums.StepStatus


class GenblazeError(Exception):
    """Base exception for all genblaze errors."""


class ProviderError(GenblazeError):
    """Raised when a provider operation fails.

    ``retry_after`` carries the server's ``Retry-After`` hint (seconds) when the
    connector parsed it from an HTTP response; the retry helper honors it over
    computed backoff, clamped to ``MAX_RETRY_AFTER_SEC``. ``attempts`` reflects
    how many tries were made before the terminal failure; populated by the
    retry helper when it exhausts its budget.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: ProviderErrorCode | None = None,
        retry_after: float | None = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.attempts = attempts


class ManifestError(GenblazeError):
    """Raised when manifest creation/validation fails."""


class EmbeddingError(GenblazeError):
    """Raised when media embedding or extraction fails."""


class SinkError(GenblazeError):
    """Raised when a sink write operation fails."""


class PipelineTimeoutError(GenblazeError):
    """Raised when a pipeline exceeds its wall-clock timeout."""


class PipelineError(GenblazeError):
    """Raised when ``Pipeline.run(raise_on_failure=True)`` and any step failed.

    Carries the partial ``PipelineResult`` so callers that want to inspect
    completed steps (or persist the failure manifest) can still do so::

        try:
            result = pipeline.run(raise_on_failure=True)
        except PipelineError as exc:
            log.error("step %d failed: %s", exc.failed_step_index, exc.failed_step_error)
            exc.result.run.steps   # partial results still available

    Defaults to ``True`` in genblaze-core 0.4.0; today (0.3.x) the silent
    "return failed result" behavior remains for callers that don't pass
    ``raise_on_failure=`` explicitly, with a ``DeprecationWarning`` describing
    the upcoming flip.
    """

    def __init__(
        self,
        message: str,
        *,
        result: PipelineResult | None = None,
        failed_step_index: int | None = None,
        failed_step_error: str | None = None,
    ):
        super().__init__(message)
        self.result: PipelineResult | None = result
        self.failed_step_index = failed_step_index
        self.failed_step_error = failed_step_error

    def __reduce__(self) -> tuple[Any, ...]:
        # Pickle-friendly — pytest serializes exceptions across boundaries.
        return (
            self.__class__,
            (str(self),),
            {
                "result": self.result,
                "failed_step_index": self.failed_step_index,
                "failed_step_error": self.failed_step_error,
            },
        )

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.result = state.get("result")
        self.failed_step_index = state.get("failed_step_index")
        self.failed_step_error = state.get("failed_step_error")


class BatchPipelineError(GenblazeError):
    """Raised by ``Pipeline.batch_run(raise_on_failure=True)`` when any item failed.

    Distinct from :class:`PipelineError` because the semantics differ: a
    single pipeline failure aborts the run, whereas a batch always **runs
    every item to completion** before raising. This lets callers salvage
    successes after a partial failure::

        try:
            results = pipeline.batch_run(items=items, raise_on_failure=True)
        except BatchPipelineError as exc:
            log.warning("%d/%d batch items failed", len(exc.failures), exc.total)
            for idx, err in exc.failures:
                log.warning(
                    "  item %d step %d: %s",
                    idx, err.failed_step_index, err.failed_step_error,
                )
            usable = exc.succeeded  # list[PipelineResult] of items that completed

    The ``results`` attribute holds **every** :class:`PipelineResult` in the
    original input order — failed items have ``run.status == FAILED``.
    The ``failures`` list is derived once at construction time so repeated
    access is O(1).
    """

    def __init__(
        self,
        results: list[PipelineResult],
        *,
        message: str | None = None,
    ):
        self.results: list[PipelineResult] = list(results)
        # Resolve enums once for the whole derivation loop — avoids re-doing
        # the sys.modules lookup per iteration.
        run_status, step_status = _enum_status()
        # Derive once: index + a synthetic PipelineError per failed result.
        # Computed eagerly because pickling needs the value, and the cost is
        # O(N) over a list that's already in memory.
        self.failures: list[tuple[int, PipelineError]] = []
        for i, r in enumerate(self.results):
            if r.run.status != run_status.FAILED:
                continue
            failed_idx: int | None = None
            failed_err: str | None = None
            for j, step in enumerate(r.run.steps):
                if step.status == step_status.FAILED:
                    failed_idx = j
                    failed_err = step.error
                    break
            self.failures.append(
                (
                    i,
                    PipelineError(
                        f"batch item {i} pipeline {r.run.run_id} failed at "
                        f"step {failed_idx}: {failed_err}",
                        result=r,
                        failed_step_index=failed_idx,
                        failed_step_error=failed_err,
                    ),
                )
            )
        if message is None:
            failed_idx_list = [i for i, _ in self.failures]
            if len(failed_idx_list) <= 10:
                idx_str = ", ".join(str(i) for i in failed_idx_list)
            else:
                idx_str = (
                    ", ".join(str(i) for i in failed_idx_list[:10])
                    + f", ... ({len(failed_idx_list)} total)"
                )
            message = (
                f"{len(self.failures)} of {len(self.results)} batch item(s) "
                f"failed (indices: {idx_str})"
            )
        super().__init__(message)

    @property
    def total(self) -> int:
        """Total number of batch items attempted (succeeded + failed)."""
        return len(self.results)

    @property
    def succeeded(self) -> list[PipelineResult]:
        """Items that completed successfully — preserves original input order.

        Computed lazily (no allocation if the caller never asks). Cached after
        first access via ``__dict__`` injection — properties on
        ``Exception`` subclasses can't use ``functools.cached_property``
        cleanly because the descriptor doesn't see ``__dict__`` reliably
        across pickle round-trips, so we cache by hand.
        """
        cached = self.__dict__.get("_succeeded_cache")
        if cached is None:
            run_status, _ = _enum_status()
            cached = [r for r in self.results if r.run.status == run_status.COMPLETED]
            self.__dict__["_succeeded_cache"] = cached
        return cached

    def __reduce__(self) -> tuple[Any, ...]:
        # Reconstruct from results only — failures + message regenerate.
        # Note: subclasses that add __init__ args must override __reduce__,
        # otherwise their extra state will be lost on unpickling.
        return (self.__class__, (self.results,))


class StorageError(GenblazeError):
    """Raised when an object storage operation fails."""


class WebhookError(GenblazeError):
    """Raised when a webhook delivery fails."""
