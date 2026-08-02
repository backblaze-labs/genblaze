"""Misplaced run-entry-point kwargs point at the call site that works.

Pipeline configuration is split across the constructor, the fluent builders,
and the run entry points. CPython's ``got an unexpected keyword argument
'cache'`` is correct but a dead end — it names neither where the option lives
nor how to spell it there. These tests pin the redirect messages and, just as
importantly, pin that genuinely unknown names keep CPython's exact wording.
"""

from __future__ import annotations

import asyncio

import pytest
from genblaze_core.models.enums import Modality
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.testing import MockProvider


def _pipeline() -> Pipeline:
    return Pipeline("misplaced-kwargs").step(
        MockProvider(), model="mock-model", prompt="hello", modality=Modality.IMAGE
    )


class TestRedirectMessages:
    def test_cache_points_at_the_fluent_builder(self):
        with pytest.raises(TypeError) as exc_info:
            _pipeline().run(cache="anything")
        msg = str(exc_info.value)
        assert "Pipeline.run() got an unexpected keyword argument 'cache'" in msg
        assert ".cache(StepCache(...))" in msg

    @pytest.mark.parametrize(
        ("kwarg", "expected_fragment"),
        [
            ("config", ".config({...})"),
            ("metadata", ".metadata(key=value)"),
            ("tracer", ".tracer(...)"),
            ("preflight", ".preflight(False)"),
            ("tenant_id", "Pipeline(..., tenant_id=...)"),
            ("project_id", "Pipeline(..., project_id=...)"),
            ("chain", "Pipeline(..., chain=...)"),
            ("moderation", "Pipeline(..., moderation=...)"),
            ("structured_log", "Pipeline(..., structured_log=...)"),
        ],
    )
    def test_constructor_and_builder_options_are_redirected(self, kwarg, expected_fragment):
        with pytest.raises(TypeError) as exc_info:
            _pipeline().run(**{kwarg: object()})
        assert expected_fragment in str(exc_info.value)

    def test_max_concurrency_redirects_only_on_the_sequential_entry_point(self):
        """run() is the one entry point without max_concurrency."""
        with pytest.raises(TypeError) as exc_info:
            _pipeline().run(max_concurrency=4)
        msg = str(exc_info.value)
        assert "arun()/abatch_run()" in msg
        assert "Pipeline(..., max_concurrency=...)" in msg

    def test_max_concurrency_is_still_a_real_kwarg_elsewhere(self):
        """The redirect table must not shadow a genuine parameter."""
        results = _pipeline().batch_run(["a", "b"], max_concurrency=1, raise_on_failure=False)
        assert len(results) == 2


class TestUnknownNamesKeepCPythonWording:
    def test_unknown_kwarg_message_is_unchanged(self):
        with pytest.raises(TypeError) as exc_info:
            _pipeline().run(definitely_not_an_option=1)
        assert (
            str(exc_info.value)
            == "Pipeline.run() got an unexpected keyword argument 'definitely_not_an_option'"
        )

    def test_unknown_kwarg_has_no_trailing_hint(self):
        with pytest.raises(TypeError) as exc_info:
            _pipeline().run(typo=1)
        assert " — " not in str(exc_info.value)


class TestEveryRunEntryPoint:
    def test_arun(self):
        with pytest.raises(TypeError, match=r"Pipeline\.arun\(\).*'cache'"):
            asyncio.run(_pipeline().arun(cache="x"))

    def test_batch_run(self):
        with pytest.raises(TypeError, match=r"Pipeline\.batch_run\(\).*'cache'"):
            _pipeline().batch_run(["a"], cache="x")

    def test_abatch_run(self):
        with pytest.raises(TypeError, match=r"Pipeline\.abatch_run\(\).*'cache'"):
            asyncio.run(_pipeline().abatch_run(["a"], cache="x"))

    def test_stream_forwards_to_run(self):
        """stream() forwards **run_kwargs, so it inherits the redirect."""
        with pytest.raises(TypeError, match=r"'cache'.*StepCache"):
            list(_pipeline().stream(cache="x"))


class TestHappyPathUnaffected:
    def test_run_still_works(self):
        result = _pipeline().run(raise_on_failure=True)
        assert result.run.steps[0].assets

    def test_run_rejects_nothing_it_used_to_accept(self):
        """Every documented run() kwarg still binds normally."""
        result = _pipeline().run(
            sink=None,
            fail_fast=True,
            raise_on_failure=False,
            timeout=30,
            max_retries=0,
            on_progress=None,
            progress=False,
            pipeline_timeout=None,
            on_step_complete=None,
            on_retry=None,
        )
        assert result.run.steps
