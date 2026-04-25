"""Coverage for ``Pipeline.run(raise_on_failure=...)`` semantics.

Three scenarios:
1. ``raise_on_failure=True`` raises ``PipelineError`` on a failed step.
2. ``raise_on_failure=False`` returns the failed ``PipelineResult`` (today's behavior).
3. ``raise_on_failure`` omitted (sentinel ``None``) emits ``DeprecationWarning``
   and behaves like ``False`` until the 0.4.0 default flip.
"""

from __future__ import annotations

import warnings

import pytest
from genblaze_core.exceptions import PipelineError
from genblaze_core.models.enums import Modality, RunStatus, StepStatus
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.testing import MockProvider


def _failing_pipeline() -> Pipeline:
    bad = MockProvider(should_fail=True, error_message="boom")
    return Pipeline("test").step(bad, model="m", prompt="x", modality=Modality.IMAGE)


def test_raise_on_failure_true_raises_pipeline_error() -> None:
    with pytest.raises(PipelineError) as excinfo:
        _failing_pipeline().run(raise_on_failure=True)
    err = excinfo.value
    assert err.failed_step_index == 0
    assert err.failed_step_error and "boom" in err.failed_step_error
    # The partial result is preserved so callers can persist the manifest.
    assert err.result is not None
    assert err.result.run.status == RunStatus.FAILED
    assert err.result.run.steps[0].status == StepStatus.FAILED


def test_raise_on_failure_false_returns_failed_result() -> None:
    result = _failing_pipeline().run(raise_on_failure=False)
    assert result.run.status == RunStatus.FAILED
    assert result.run.steps[0].status == StepStatus.FAILED


def test_raise_on_failure_omitted_warns_and_returns_failed_result() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        result = _failing_pipeline().run()
    assert result.run.status == RunStatus.FAILED
    msg_texts = [str(w.message) for w in captured if issubclass(w.category, DeprecationWarning)]
    assert any("raise PipelineError" in m for m in msg_texts), (
        f"Expected DeprecationWarning about raise_on_failure; got: {msg_texts}"
    )


def test_raise_on_failure_does_not_raise_on_success() -> None:
    ok = MockProvider()
    pipeline = Pipeline("test").step(ok, model="m", prompt="x", modality=Modality.IMAGE)
    result = pipeline.run(raise_on_failure=True)
    assert result.run.status == RunStatus.COMPLETED
