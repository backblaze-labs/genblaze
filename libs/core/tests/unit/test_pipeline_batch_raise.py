"""Coverage for ``Pipeline.batch_run`` collect-then-raise semantics.

Behavior under test (final design after the 0.4.0 deprecation cycle):

- ``raise_on_failure=False``: returns ``list[PipelineResult]`` with mixed
  succeeded / failed items. No exception raised.
- ``raise_on_failure=True``: every item runs to completion (never aborts
  mid-batch); after collection, if any item failed, ``BatchPipelineError``
  is raised with all results attached so callers can salvage successes.
- ``raise_on_failure`` omitted (sentinel ``None``): emits a single
  ``DeprecationWarning`` mentioning ``BatchPipelineError`` and behaves
  like ``False`` until 0.4.0.

The "every item runs" guarantee is the key DX promise — losing items 4-N
because item 3 failed is the worst possible outcome for asset-pack /
A-B / sweep workflows.
"""

from __future__ import annotations

import asyncio
import pickle
import warnings

import pytest
from genblaze_core.exceptions import (
    BatchPipelineError,
    PipelineError,
    PipelineTimeoutError,
    ProviderError,
)
from genblaze_core.models.enums import Modality, RunStatus
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.testing import MockProvider


def _provider_failing_on_prompts(*fail_prompts: str) -> MockProvider:
    """A MockProvider that fails when ``step.prompt`` matches any given prompt.

    Prompt-based failure signaling avoids the call-counter race that would
    otherwise make ordering nondeterministic under ``asyncio.gather`` —
    each item's outcome is determined solely by its own prompt, regardless
    of which order the scheduler runs them in.
    """
    provider = MockProvider()
    original_generate = provider.generate
    fail_set = set(fail_prompts)

    def _flaky_generate(step, config=None):
        if step.prompt in fail_set:
            raise ProviderError(f"simulated failure for prompt {step.prompt!r}")
        return original_generate(step, config)

    provider.generate = _flaky_generate  # type: ignore[method-assign]
    return provider


# --- Core collect-then-raise contract --------------------------------------


def test_batch_run_raise_collects_all_results_then_raises() -> None:
    """The 2nd item fails but items 3+ still run, all results land in the exception."""
    provider = _provider_failing_on_prompts("1")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    items = [{"prompt": str(i)} for i in range(4)]
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(items=items, raise_on_failure=True)
    err = excinfo.value
    assert err.total == 4
    assert len(err.results) == 4
    assert len(err.failures) == 1
    failure_idx, failure_err = err.failures[0]
    assert failure_idx == 1
    assert isinstance(failure_err, PipelineError)
    assert "simulated failure" in (failure_err.failed_step_error or "")
    assert len(err.succeeded) == 3
    assert all(r.run.status == RunStatus.COMPLETED for r in err.succeeded)


def test_batch_run_raise_skipped_when_no_failures() -> None:
    """Pure-success batches return the list even with raise_on_failure=True."""
    provider = MockProvider()
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    results = pipe.batch_run(items=[{"prompt": "a"}, {"prompt": "b"}], raise_on_failure=True)
    assert len(results) == 2
    assert all(r.run.status == RunStatus.COMPLETED for r in results)


def test_batch_run_no_raise_returns_failed_results() -> None:
    """raise_on_failure=False keeps today's behavior: caller inspects the list."""
    provider = _provider_failing_on_prompts("1")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    results = pipe.batch_run(items=[{"prompt": str(i)} for i in range(3)], raise_on_failure=False)
    assert len(results) == 3
    statuses = [r.run.status for r in results]
    assert statuses.count(RunStatus.FAILED) == 1
    assert statuses.count(RunStatus.COMPLETED) == 2


def test_batch_run_omitted_raise_warns_about_batch_pipeline_error() -> None:
    """The DeprecationWarning must mention BatchPipelineError, not PipelineError."""
    provider = MockProvider()
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        pipe.batch_run(items=[{"prompt": "x"}])
    msgs = [str(w.message) for w in captured if issubclass(w.category, DeprecationWarning)]
    assert any("BatchPipelineError" in m for m in msgs), msgs
    batch_warnings = [m for m in msgs if "batch_run" in m]
    assert len(batch_warnings) == 1


def test_batch_pipeline_error_message_lists_failed_indices() -> None:
    provider = _provider_failing_on_prompts("1")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(items=[{"prompt": str(i)} for i in range(3)], raise_on_failure=True)
    msg = str(excinfo.value)
    assert "1 of 3" in msg
    assert "indices: 1" in msg


# --- Async parity ----------------------------------------------------------


def test_async_abatch_run_raise_collects_all() -> None:
    """abatch_run mirrors batch_run: gather all then raise."""
    provider = _provider_failing_on_prompts("1")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)

    async def _go():
        with pytest.raises(BatchPipelineError) as excinfo:
            await pipe.abatch_run(
                items=[{"prompt": str(i)} for i in range(4)],
                raise_on_failure=True,
                max_concurrency=4,
            )
        return excinfo.value

    err = asyncio.run(_go())
    assert err.total == 4
    assert len(err.failures) == 1
    # Failure index is deterministic because failure is prompt-based, not
    # call-order based — item 1 has prompt "1" and is the only failure
    # regardless of which order asyncio schedules the tasks.
    assert err.failures[0][0] == 1
    assert sum(1 for r in err.results if r.run.status == RunStatus.COMPLETED) == 3


def test_async_abatch_run_does_not_cancel_on_pipeline_timeout(monkeypatch) -> None:
    """A PipelineTimeoutError on one item must not cancel the rest mid-flight.

    Regression guard for the H1 finding: prior to the ``return_exceptions=True``
    fix, ``asyncio.gather`` propagated the timeout immediately and cancelled
    every other in-flight task, silently breaking the "every item runs"
    promise. The user got a timeout exception with zero results.
    """
    provider = MockProvider()
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)

    # Patch arun on a per-instance basis: the second call raises
    # PipelineTimeoutError, the rest succeed normally.
    real_arun = Pipeline.arun
    call_id = {"n": 0}

    async def _patched_arun(self, **kw):
        call_id["n"] += 1
        if call_id["n"] == 2:
            raise PipelineTimeoutError("simulated timeout for item 1")
        return await real_arun(self, **kw)

    monkeypatch.setattr(Pipeline, "arun", _patched_arun)

    async def _go():
        return await pipe.abatch_run(
            items=[{"prompt": str(i)} for i in range(4)],
            raise_on_failure=False,
            max_concurrency=1,  # serialize to control which task is "second"
        )

    with pytest.raises(PipelineTimeoutError):
        asyncio.run(_go())
    # All 4 tasks must have been started — confirms gather did not cancel
    # in-flight tasks when one raised.
    assert call_id["n"] == 4


# --- Edge cases (L1 coverage) ----------------------------------------------


def test_succeeded_property_returns_completed_results() -> None:
    """``.succeeded`` is the source of truth for items the caller can use."""
    provider = _provider_failing_on_prompts("1", "3")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(items=[{"prompt": str(i)} for i in range(4)], raise_on_failure=True)
    err = excinfo.value
    succeeded = err.succeeded
    assert len(succeeded) == 2
    assert all(r.run.status == RunStatus.COMPLETED for r in succeeded)
    # Cached on second access — same list object returned.
    assert err.succeeded is succeeded


def test_succeeded_plus_failures_equals_total() -> None:
    """Invariant: every result is either in succeeded or failures, not both."""
    provider = _provider_failing_on_prompts("1", "2")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(items=[{"prompt": str(i)} for i in range(5)], raise_on_failure=True)
    err = excinfo.value
    assert len(err.succeeded) + len(err.failures) == err.total


def test_all_items_fail() -> None:
    """When every item fails, BatchPipelineError still carries all results."""
    provider = _provider_failing_on_prompts("a", "b", "c")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(items=[{"prompt": p} for p in ("a", "b", "c")], raise_on_failure=True)
    err = excinfo.value
    assert err.total == 3
    assert len(err.failures) == 3
    assert len(err.succeeded) == 0
    assert [idx for idx, _ in err.failures] == [0, 1, 2]


def test_interleaved_failure_indices() -> None:
    """Failures at non-contiguous positions retain their original input indices."""
    provider = _provider_failing_on_prompts("0", "2", "4")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(items=[{"prompt": str(i)} for i in range(5)], raise_on_failure=True)
    err = excinfo.value
    assert [idx for idx, _ in err.failures] == [0, 2, 4]


def test_batch_run_prompts_path_with_raise_on_failure() -> None:
    """The legacy prompts= overload also collects-and-raises."""
    provider = _provider_failing_on_prompts("y")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    with pytest.raises(BatchPipelineError) as excinfo:
        pipe.batch_run(prompts=["x", "y", "z"], raise_on_failure=True)
    err = excinfo.value
    assert err.total == 3
    assert len(err.failures) == 1
    assert err.failures[0][0] == 1


# --- Pickle / serialization ------------------------------------------------


def test_batch_pipeline_error_pickles() -> None:
    """xdist / multiprocessing workers serialize exceptions."""
    provider = _provider_failing_on_prompts("1")
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    try:
        pipe.batch_run(items=[{"prompt": str(i)} for i in range(2)], raise_on_failure=True)
    except BatchPipelineError as exc:
        blob = pickle.dumps(exc)
        restored = pickle.loads(blob)
        assert isinstance(restored, BatchPipelineError)
        assert restored.total == exc.total
        assert len(restored.failures) == len(exc.failures)
        assert restored.failures[0][0] == exc.failures[0][0]
