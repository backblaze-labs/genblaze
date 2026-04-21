"""Tests for Pipeline API."""

from pathlib import Path
from typing import Any

import pytest
from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import RunStatus, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.pipeline import Pipeline, StepCache
from genblaze_core.pipeline.result import PipelineResult
from genblaze_core.providers.base import BaseProvider
from genblaze_core.runnable.config import RunnableConfig


class MockProvider(BaseProvider):
    """Provider that always succeeds with a single asset."""

    name = "mock"

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        return "pred-123"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
        return step


def test_pipeline_single_step() -> None:
    provider = MockProvider()
    result = Pipeline("test").step(provider, model="test-model", prompt="a cat").run()

    assert isinstance(result, PipelineResult)
    assert result.run.name == "test"
    assert len(result.run.steps) == 1
    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert len(result.run.steps[0].assets) == 1
    assert result.manifest.verify()
    assert result.run.status == RunStatus.COMPLETED


def test_pipeline_result_tuple_unpacking() -> None:
    """PipelineResult supports run, manifest = pipeline.run() destructuring."""
    provider = MockProvider()
    run, manifest = Pipeline("unpack").step(provider, model="m", prompt="p").run()

    assert run.name == "unpack"
    assert manifest.verify()
    assert run.steps[0].status == StepStatus.SUCCEEDED


def test_pipeline_result_repr() -> None:
    provider = MockProvider()
    result = Pipeline("repr-test").step(provider, model="m", prompt="p").run()
    assert "PipelineResult" in repr(result)
    assert result.run.run_id in repr(result)


def test_pipeline_multi_step() -> None:
    provider = MockProvider()
    result = (
        Pipeline("multi")
        .step(provider, model="model-a", prompt="step 1")
        .step(provider, model="model-b", prompt="step 2")
        .run()
    )

    assert len(result.run.steps) == 2
    assert all(s.status == StepStatus.SUCCEEDED for s in result.run.steps)
    assert result.manifest.verify()
    assert result.run.status == RunStatus.COMPLETED


# --- Step cache tests ---


class CountingProvider(MockProvider):
    """Provider that counts how many times invoke is called."""

    def __init__(self) -> None:
        super().__init__()
        self.invoke_count = 0

    def submit(self, step, config=None) -> Any:
        self.invoke_count += 1
        return super().submit(step, config)


def test_pipeline_cache_hit(tmp_path: Path) -> None:
    """Second run with same params should use cache, not call provider."""
    provider = CountingProvider()
    cache = StepCache(tmp_path / "cache")

    # First run — cache miss, provider called
    Pipeline("cached").cache(cache).step(provider, model="m", prompt="p").run()
    assert provider.invoke_count == 1

    # Second run — cache hit, provider not called again
    provider2 = CountingProvider()
    Pipeline("cached").cache(cache).step(provider2, model="m", prompt="p").run()
    assert provider2.invoke_count == 0


def test_pipeline_cache_miss_different_params(tmp_path: Path) -> None:
    """Different params should produce cache miss."""
    cache = StepCache(tmp_path / "cache")
    provider = CountingProvider()

    Pipeline("c").cache(cache).step(provider, model="m", prompt="a").run()
    assert provider.invoke_count == 1

    Pipeline("c").cache(cache).step(provider, model="m", prompt="b").run()
    assert provider.invoke_count == 2


def test_pipeline_cache_miss_different_negative_prompt(tmp_path: Path) -> None:
    """Steps that differ only in negative_prompt must get distinct cache entries."""
    from genblaze_core.models.step import Step
    from genblaze_core.pipeline.cache import step_cache_key

    a = Step(provider="p", model="m", prompt="same", negative_prompt="red")
    b = Step(provider="p", model="m", prompt="same", negative_prompt="blue")
    assert step_cache_key(a) != step_cache_key(b)


def test_pipeline_cache_miss_different_model_version(tmp_path: Path) -> None:
    """Steps with different model_version must not share a cache entry."""
    from genblaze_core.models.step import Step
    from genblaze_core.pipeline.cache import step_cache_key

    a = Step(provider="p", model="m", prompt="p", model_version="v1")
    b = Step(provider="p", model="m", prompt="p", model_version="v2")
    assert step_cache_key(a) != step_cache_key(b)


@pytest.mark.parametrize(
    ("field", "val_a", "val_b"),
    [
        ("model_hash", "abc123", "def456"),
        pytest.param(
            "prompt_visibility",
            "public",
            "private",
            id="prompt_visibility",
        ),
        pytest.param("step_type", "generate", "upscale", id="step_type"),
        pytest.param("modality", "image", "video", id="modality"),
    ],
)
def test_pipeline_cache_miss_different_new_fields(field: str, val_a: str, val_b: str) -> None:
    """Each new cache-key field must flip the key when changed in isolation.

    Locks in the 4 fields added in b642f6a (model_hash, prompt_visibility,
    step_type, modality) so they cannot be silently dropped from
    step_cache_key without a test failure.
    """
    from genblaze_core.models.step import Step
    from genblaze_core.pipeline.cache import step_cache_key

    a = Step(provider="p", model="m", prompt="same", **{field: val_a})
    b = Step(provider="p", model="m", prompt="same", **{field: val_b})
    assert step_cache_key(a) != step_cache_key(b)


def test_pipeline_cache_clear(tmp_path: Path) -> None:
    """Cache.clear() should invalidate all entries."""
    cache = StepCache(tmp_path / "cache")
    provider = CountingProvider()

    Pipeline("c").cache(cache).step(provider, model="m", prompt="p").run()
    assert provider.invoke_count == 1

    cache.clear()

    Pipeline("c").cache(cache).step(provider, model="m", prompt="p").run()
    assert provider.invoke_count == 2


# --- Async pipeline tests ---


@pytest.mark.asyncio
async def test_pipeline_arun() -> None:
    """Pipeline.arun() executes steps asynchronously."""
    provider = MockProvider()
    result = await Pipeline("async-test").step(provider, model="m", prompt="p").arun()

    assert isinstance(result, PipelineResult)
    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert result.manifest.verify()


@pytest.mark.asyncio
async def test_pipeline_arun_with_cache(tmp_path: Path) -> None:
    """Async pipeline should respect cache."""
    cache = StepCache(tmp_path / "cache")
    provider = CountingProvider()

    await Pipeline("ac").cache(cache).step(provider, model="m", prompt="p").arun()
    assert provider.invoke_count == 1

    provider2 = CountingProvider()
    await Pipeline("ac").cache(cache).step(provider2, model="m", prompt="p").arun()
    assert provider2.invoke_count == 0


# --- Fail-fast tests ---


class FailingProvider(BaseProvider):
    """Provider that always fails."""

    name = "failing"

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        raise RuntimeError("Provider failed")

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        return step


def test_pipeline_fail_fast_stops_on_failure() -> None:
    """With fail_fast=True (default), pipeline stops after first failed step."""
    failing = FailingProvider()
    counter = CountingProvider()

    result = (
        Pipeline("fail-fast")
        .step(failing, model="m", prompt="will fail")
        .step(counter, model="m", prompt="should not run")
        .run()
    )

    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 1
    assert counter.invoke_count == 0


def test_pipeline_fail_fast_false_continues() -> None:
    """With fail_fast=False, pipeline continues after failed step."""
    failing = FailingProvider()
    counter = CountingProvider()

    result = (
        Pipeline("no-fail-fast")
        .step(failing, model="m", prompt="will fail")
        .step(counter, model="m", prompt="should run")
        .run(fail_fast=False)
    )

    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 2
    assert counter.invoke_count == 1


# --- Empty pipeline guard ---


def test_pipeline_empty_raises() -> None:
    """Pipeline.run() raises GenblazeError when no steps are added."""
    with pytest.raises(GenblazeError, match="no steps"):
        Pipeline("empty").run()


@pytest.mark.asyncio
async def test_pipeline_empty_arun_raises() -> None:
    """Pipeline.arun() raises GenblazeError when no steps are added."""
    with pytest.raises(GenblazeError, match="no steps"):
        await Pipeline("empty").arun()


def test_pipeline_ctor_rejects_invalid_max_concurrency() -> None:
    """Pipeline(max_concurrency < 1) must raise at construction time."""
    with pytest.raises(GenblazeError, match="max_concurrency"):
        Pipeline("ctor", max_concurrency=0)
    with pytest.raises(GenblazeError, match="max_concurrency"):
        Pipeline("ctor", max_concurrency=-1)


@pytest.mark.asyncio
async def test_pipeline_arun_rejects_invalid_max_concurrency() -> None:
    """arun(max_concurrency < 1) must fail before any tracer events are emitted.

    Previously the guard fired inside the concurrent branch, after run_start
    and per-step step_start events had already been written.
    """
    pipeline = Pipeline("mc").step(MockProvider(), model="m", prompt="p")
    with pytest.raises(GenblazeError, match="max_concurrency"):
        await pipeline.arun(max_concurrency=0)
    with pytest.raises(GenblazeError, match="max_concurrency"):
        await pipeline.arun(max_concurrency=-1)


# --- PipelineResult.save tests ---


def test_pipeline_result_save_sidecar(tmp_path: Path) -> None:
    """save(embed=False) should write a sidecar file."""
    from PIL import Image

    provider = MockProvider()
    result = Pipeline("save-test").step(provider, model="m", prompt="p").run()

    png = tmp_path / "output.png"
    Image.new("RGB", (1, 1)).save(png)

    embed_result = result.save(png, embed=False)
    assert embed_result.method == "sidecar"
    assert embed_result.sidecar_path is not None
    assert embed_result.sidecar_path.exists()


# --- Step chaining tests ---


class ChainableProvider(BaseProvider):
    """Provider that records inputs and produces predictable outputs."""

    name = "chainable"

    def __init__(self, output_url: str = "https://example.com/chained.png"):
        super().__init__()
        self.output_url = output_url
        self.received_inputs: list[list[Asset]] = []

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.received_inputs.append(list(step.inputs))
        return "pred-chain"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url=self.output_url, media_type="image/png"))
        return step


def test_chain_passes_outputs_as_inputs() -> None:
    """With chain=True, each step receives previous step's assets as inputs."""
    p1 = ChainableProvider(output_url="https://example.com/step1.png")
    p2 = ChainableProvider(output_url="https://example.com/step2.png")

    result = (
        Pipeline("chain-test", chain=True)
        .step(p1, model="m1", prompt="first")
        .step(p2, model="m2", prompt="second")
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    # First step has no inputs
    assert p1.received_inputs[0] == []
    # Second step received first step's output as input
    assert len(p2.received_inputs[0]) == 1
    assert p2.received_inputs[0][0].url == "https://example.com/step1.png"


def test_chain_false_no_inputs_passed() -> None:
    """With chain=False (default), steps don't receive previous outputs."""
    p1 = ChainableProvider()
    p2 = ChainableProvider()

    Pipeline("no-chain").step(p1, model="m", prompt="a").step(p2, model="m", prompt="b").run()

    assert p1.received_inputs[0] == []
    assert p2.received_inputs[0] == []


def test_chain_failure_stops_propagation() -> None:
    """When a chained step fails, subsequent steps don't run (fail_fast)."""
    failing = FailingProvider()
    counter = CountingProvider()

    result = (
        Pipeline("chain-fail", chain=True)
        .step(failing, model="m", prompt="will fail")
        .step(counter, model="m", prompt="should not run")
        .run()
    )

    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 1
    assert counter.invoke_count == 0


# --- Concurrent arun tests ---


@pytest.mark.asyncio
async def test_arun_concurrent_when_not_chained() -> None:
    """arun() with chain=False runs all steps concurrently."""
    p1 = CountingProvider()
    p2 = CountingProvider()

    result = await (
        Pipeline("concurrent")
        .step(p1, model="m1", prompt="a")
        .step(p2, model="m2", prompt="b")
        .arun()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert len(result.run.steps) == 2
    assert p1.invoke_count == 1
    assert p2.invoke_count == 1


@pytest.mark.asyncio
async def test_arun_chained_sequential() -> None:
    """arun() with chain=True runs steps sequentially, passing outputs."""
    p1 = ChainableProvider(output_url="https://example.com/async1.png")
    p2 = ChainableProvider(output_url="https://example.com/async2.png")

    result = await (
        Pipeline("async-chain", chain=True)
        .step(p1, model="m1", prompt="first")
        .step(p2, model="m2", prompt="second")
        .arun()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert p1.received_inputs[0] == []
    assert len(p2.received_inputs[0]) == 1


@pytest.mark.asyncio
async def test_arun_concurrent_preserves_order() -> None:
    """arun() with fail_fast=True preserves original step order in results."""
    p1 = ChainableProvider(output_url="https://example.com/first.png")
    p2 = ChainableProvider(output_url="https://example.com/second.png")
    p3 = ChainableProvider(output_url="https://example.com/third.png")

    result = await (
        Pipeline("order")
        .step(p1, model="m1", prompt="a")
        .step(p2, model="m2", prompt="b")
        .step(p3, model="m3", prompt="c")
        .arun()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert len(result.run.steps) == 3
    assert result.run.steps[0].assets[0].url == "https://example.com/first.png"
    assert result.run.steps[1].assets[0].url == "https://example.com/second.png"
    assert result.run.steps[2].assets[0].url == "https://example.com/third.png"


@pytest.mark.asyncio
async def test_arun_fail_fast_cancelled_steps_preserved() -> None:
    """Concurrent fail-fast must return all steps, including cancelled ones."""

    class AsyncFailingProvider(BaseProvider):
        """Provider whose submit raises immediately."""

        name = "async-fail"

        def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
            raise RuntimeError("instant failure")

        def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
            return True

        def fetch_output(self, prediction_id: Any, step: Step) -> Step:
            return step

    class AsyncSlowProvider(BaseProvider):
        """Provider that takes time in submit (async-compatible via sleep)."""

        name = "async-slow"

        def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
            import time

            time.sleep(0.5)
            return "pred-slow"

        def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
            return True

        def fetch_output(self, prediction_id: Any, step: Step) -> Step:
            step.assets.append(Asset(url="https://example.com/slow.png", media_type="image/png"))
            return step

    result = await (
        Pipeline("fail-fast-cancel")
        .step(AsyncFailingProvider(), model="m1", prompt="will fail")
        .step(AsyncSlowProvider(), model="m2", prompt="will be cancelled")
        .arun()
    )

    # Both steps must be present — cancelled step gets a FAILED placeholder
    assert len(result.run.steps) == 2
    assert result.run.status == RunStatus.FAILED
    assert result.run.steps[0].status == StepStatus.FAILED
    assert result.run.steps[1].status == StepStatus.FAILED


# --- timeout/max_retries kwargs ---


def test_pipeline_run_timeout_kwarg() -> None:
    """Pipeline.run(timeout=...) builds a RunnableConfig internally."""
    provider = MockProvider()
    result = Pipeline("timeout").step(provider, model="m", prompt="p").run(timeout=60)
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


def test_pipeline_run_max_retries_kwarg() -> None:
    """Pipeline.run(max_retries=...) builds a RunnableConfig internally."""
    provider = MockProvider()
    result = Pipeline("retries").step(provider, model="m", prompt="p").run(max_retries=2)
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


# --- Runnable conformance tests ---


def test_pipeline_invoke_delegates_to_run() -> None:
    """Pipeline.invoke() delegates to run()."""
    provider = MockProvider()
    pipe = Pipeline("runnable-test").step(provider, model="m", prompt="p")
    result = pipe.invoke()

    assert isinstance(result, PipelineResult)
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_pipeline_ainvoke_delegates_to_arun() -> None:
    """Pipeline.ainvoke() delegates to arun()."""
    provider = MockProvider()
    pipe = Pipeline("async-runnable").step(provider, model="m", prompt="p")
    result = await pipe.ainvoke()

    assert isinstance(result, PipelineResult)
    assert result.run.steps[0].status == StepStatus.SUCCEEDED


def test_pipeline_is_runnable() -> None:
    """Pipeline is a Runnable subclass."""
    from genblaze_core.runnable.base import Runnable

    assert issubclass(Pipeline, Runnable)


# --- New Pipeline features ---


def test_pipeline_project_id() -> None:
    """Pipeline(project_id=...) sets project_id on the resulting Run."""
    provider = MockProvider()
    pipe = Pipeline("proj-test", project_id="proj-123")
    result = pipe.step(provider, model="m", prompt="p").run()
    assert result.run.project_id == "proj-123"


def test_pipeline_run_timestamps() -> None:
    """Pipeline.run() sets started_at and completed_at on the Run."""
    provider = MockProvider()
    result = Pipeline("ts-test").step(provider, model="m", prompt="p").run()
    assert result.run.started_at is not None
    assert result.run.completed_at is not None
    assert result.run.started_at <= result.run.completed_at


def test_pipeline_step_index_set() -> None:
    """Pipeline.run() assigns step_index to each step via RunBuilder."""
    provider = MockProvider()
    result = (
        Pipeline("idx-test")
        .step(provider, model="m1", prompt="a")
        .step(provider, model="m2", prompt="b")
        .run()
    )
    assert result.run.steps[0].step_index == 0
    assert result.run.steps[1].step_index == 1


@pytest.mark.asyncio
async def test_pipeline_arun_timestamps() -> None:
    """Pipeline.arun() sets started_at and completed_at on the Run."""
    provider = MockProvider()
    result = await Pipeline("async-ts").step(provider, model="m", prompt="p").arun()
    assert result.run.started_at is not None
    assert result.run.completed_at is not None


# --- Iteration / lineage tests ---


def test_pipeline_from_result_sets_parent_run_id() -> None:
    """from_result() links the new run to the previous one."""
    provider = MockProvider()
    v1 = Pipeline("iter-test").step(provider, model="m", prompt="first attempt").run()

    v2 = (
        Pipeline("iter-test")
        .from_result(v1)
        .step(provider, model="m", prompt="refined attempt")
        .run()
    )

    assert v2.run.parent_run_id == v1.run.run_id
    assert v2.manifest.verify()
    # parent_run_id doesn't affect the hash
    assert v2.run.run_id != v1.run.run_id


def test_pipeline_from_result_chain() -> None:
    """Iteration chain: v1 → v2 → v3, each linked to its parent."""
    provider = MockProvider()
    v1 = Pipeline("chain").step(provider, model="m", prompt="v1").run()
    v2 = Pipeline("chain").from_result(v1).step(provider, model="m", prompt="v2").run()
    v3 = Pipeline("chain").from_result(v2).step(provider, model="m", prompt="v3").run()

    assert v1.run.parent_run_id is None
    assert v2.run.parent_run_id == v1.run.run_id
    assert v3.run.parent_run_id == v2.run.run_id


def test_pipeline_from_result_preserves_in_manifest() -> None:
    """parent_run_id appears in the full manifest JSON."""
    import json

    provider = MockProvider()
    v1 = Pipeline("manifest-test").step(provider, model="m", prompt="p").run()
    v2 = Pipeline("manifest-test").from_result(v1).step(provider, model="m", prompt="p2").run()

    manifest_json = json.loads(v2.manifest.to_canonical_json())
    assert manifest_json["run"]["parent_run_id"] == v1.run.run_id


def test_pipeline_no_parent_by_default() -> None:
    """Without from_result(), parent_run_id is None."""
    provider = MockProvider()
    result = Pipeline("no-parent").step(provider, model="m", prompt="p").run()
    assert result.run.parent_run_id is None


@pytest.mark.asyncio
async def test_pipeline_from_result_async() -> None:
    """from_result() works with arun() too."""
    provider = MockProvider()
    v1 = await Pipeline("async-iter").step(provider, model="m", prompt="p").arun()
    v2 = await Pipeline("async-iter").from_result(v1).step(provider, model="m", prompt="p2").arun()
    assert v2.run.parent_run_id == v1.run.run_id


# --- Progress callback tests ---


def test_pipeline_on_progress_fires() -> None:
    """on_progress callback receives events during run()."""
    from genblaze_core.providers.progress import ProgressEvent

    events: list[ProgressEvent] = []
    provider = MockProvider()
    Pipeline("progress-test").step(provider, model="m", prompt="p").run(on_progress=events.append)

    # MockProvider polls once (immediate success) → submitted + succeeded
    assert len(events) >= 2
    statuses = [e.status for e in events]
    assert "submitted" in statuses
    assert "succeeded" in statuses
    assert all(e.provider == "mock" for e in events)
    assert all(e.model == "m" for e in events)


def test_pipeline_on_progress_none_ok() -> None:
    """on_progress=None (default) doesn't break anything."""
    provider = MockProvider()
    result = Pipeline("no-progress").step(provider, model="m", prompt="p").run()
    assert result.run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_pipeline_arun_on_progress_fires() -> None:
    """on_progress callback receives events during arun()."""
    from genblaze_core.providers.progress import ProgressEvent

    events: list[ProgressEvent] = []
    provider = MockProvider()
    await (
        Pipeline("async-progress")
        .step(provider, model="m", prompt="p")
        .arun(on_progress=events.append)
    )

    assert len(events) >= 2
    statuses = [e.status for e in events]
    assert "submitted" in statuses
    assert "succeeded" in statuses


# --- Progress "failed" event tests ---


def test_progress_emits_failed_on_error() -> None:
    """on_progress callback fires 'failed' when a step fails."""
    from genblaze_core.providers.progress import ProgressEvent

    events: list[ProgressEvent] = []
    failing = FailingProvider()
    Pipeline("fail-progress").step(failing, model="m", prompt="p").run(on_progress=events.append)

    statuses = [e.status for e in events]
    assert "failed" in statuses


# --- batch_run / abatch_run tests ---


def test_batch_run_returns_ordered_results() -> None:
    """batch_run() returns results in the same order as prompts."""
    provider = MockProvider()
    prompts = ["alpha", "beta", "gamma"]
    results = Pipeline("batch").step(provider, model="m").batch_run(prompts)

    assert len(results) == 3
    for r in results:
        assert isinstance(r, PipelineResult)
        assert r.run.status == RunStatus.COMPLETED
        assert r.manifest.verify()


def test_batch_run_independent_runs() -> None:
    """Each batch prompt gets its own independent run_id."""
    provider = MockProvider()
    results = Pipeline("batch-ids").step(provider, model="m").batch_run(["a", "b"])

    run_ids = [r.run.run_id for r in results]
    assert run_ids[0] != run_ids[1]


def test_batch_run_forwards_config() -> None:
    """batch_run forwards fail_fast, timeout, max_retries to inner runs."""
    provider = MockProvider()
    results = (
        Pipeline("batch-cfg").step(provider, model="m").batch_run(["a"], timeout=60, max_retries=2)
    )
    assert results[0].run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_abatch_run_returns_ordered_results() -> None:
    """abatch_run() returns results in prompt order."""
    provider = MockProvider()
    results = (
        await Pipeline("abatch").step(provider, model="m").abatch_run(["alpha", "beta", "gamma"])
    )

    assert len(results) == 3
    for r in results:
        assert r.run.status == RunStatus.COMPLETED
        assert r.manifest.verify()


@pytest.mark.asyncio
async def test_abatch_run_respects_concurrency() -> None:
    """abatch_run() limits concurrency via semaphore."""
    provider = MockProvider()
    results = (
        await Pipeline("abatch-sem")
        .step(provider, model="m")
        .abatch_run(["a", "b", "c", "d"], max_concurrency=2)
    )
    assert len(results) == 4


# --- Fallback model tests ---


class ModelErrorProvider(BaseProvider):
    """Provider that fails with MODEL_ERROR for specific models."""

    name = "model-err"

    def __init__(self, failing_models: set[str]):
        super().__init__()
        self.failing_models = failing_models
        self.invoked_models: list[str] = []

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.invoked_models.append(step.model)
        if step.model in self.failing_models:
            from genblaze_core.exceptions import ProviderError
            from genblaze_core.models.enums import ProviderErrorCode

            raise ProviderError(
                f"Model {step.model} not found error",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )
        return "pred-ok"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
        return step


def test_fallback_model_on_model_error() -> None:
    """Pipeline falls back to alternate model on MODEL_ERROR."""
    provider = ModelErrorProvider(failing_models={"bad-model"})
    result = (
        Pipeline("fallback")
        .step(
            provider,
            model="bad-model",
            prompt="test",
            fallback_models=["good-model"],
        )
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert "bad-model" in provider.invoked_models
    assert "good-model" in provider.invoked_models


def test_fallback_model_all_fail() -> None:
    """When all fallback models also fail, step is FAILED."""
    provider = ModelErrorProvider(failing_models={"m1", "m2", "m3"})
    result = (
        Pipeline("fallback-exhaust")
        .step(
            provider,
            model="m1",
            prompt="test",
            fallback_models=["m2", "m3"],
        )
        .run()
    )

    assert result.run.status == RunStatus.FAILED
    assert len(provider.invoked_models) == 3


def test_fallback_not_triggered_on_non_model_error() -> None:
    """Fallback is NOT triggered for non-MODEL_ERROR failures."""
    failing = FailingProvider()

    result = (
        Pipeline("no-fallback")
        .step(
            failing,
            model="m",
            prompt="test",
            fallback_models=["backup-model"],
        )
        .run()
    )

    # FailingProvider raises RuntimeError → classified as UNKNOWN, not MODEL_ERROR
    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 1


def test_fallback_cache_keys_correct(tmp_path: Path) -> None:
    """Cache stores fallback result keyed by fallback model, not original."""
    cache = StepCache(tmp_path / "cache")
    provider = ModelErrorProvider(failing_models={"bad-model"})

    # Run with fallback: bad-model fails, good-model succeeds
    Pipeline("fb-cache").cache(cache).step(
        provider, model="bad-model", prompt="test", fallback_models=["good-model"]
    ).run()

    # Cache should NOT have an entry for bad-model (it failed)
    bad_step = Step(provider="model-err", model="bad-model", prompt="test")
    assert cache.get(bad_step) is None

    # Cache SHOULD have an entry for good-model (it succeeded)
    good_step = Step(provider="model-err", model="good-model", prompt="test")
    cached = cache.get(good_step)
    assert cached is not None
    assert cached.status == StepStatus.SUCCEEDED


# --- Pipeline timeout and on_step_complete tests ---


class SlowProvider(BaseProvider):
    """Provider that sleeps for a configurable duration."""

    name = "slow"

    def __init__(self, delay: float = 0.0):
        super().__init__()
        self.delay = delay

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        import time

        time.sleep(self.delay)
        return "pred-slow"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/slow.png", media_type="image/png"))
        return step


def test_pipeline_timeout_triggers() -> None:
    """pipeline_timeout raises GenblazeError when total time exceeds limit."""
    # Each step takes ~0.1s; 3 steps = ~0.3s; timeout at 0.15s
    slow = SlowProvider(delay=0.1)
    with pytest.raises(GenblazeError, match="Pipeline timeout exceeded"):
        (
            Pipeline("timeout-test")
            .step(slow, model="m", prompt="a")
            .step(slow, model="m", prompt="b")
            .step(slow, model="m", prompt="c")
            .run(pipeline_timeout=0.15)
        )


def test_pipeline_timeout_within_limit() -> None:
    """pipeline_timeout does not trigger when pipeline finishes in time."""
    provider = MockProvider()
    result = (
        Pipeline("timeout-ok")
        .step(provider, model="m", prompt="a")
        .step(provider, model="m", prompt="b")
        .run(pipeline_timeout=10.0)
    )
    assert result.run.status == RunStatus.COMPLETED


def test_on_step_complete_fires_for_each_step() -> None:
    """on_step_complete fires for each step with correct index and total."""
    from genblaze_core.pipeline.result import StepCompleteEvent

    events: list[StepCompleteEvent] = []
    provider = MockProvider()
    (
        Pipeline("callback-test")
        .step(provider, model="m1", prompt="a")
        .step(provider, model="m2", prompt="b")
        .step(provider, model="m3", prompt="c")
        .run(on_step_complete=events.append)
    )

    assert len(events) == 3
    assert [e.step_index for e in events] == [0, 1, 2]
    assert all(e.total_steps == 3 for e in events)
    assert all(e.elapsed_sec > 0 for e in events)
    # Elapsed should be monotonically increasing
    assert events[0].elapsed_sec <= events[1].elapsed_sec <= events[2].elapsed_sec


def test_on_step_complete_fires_for_failed_steps() -> None:
    """on_step_complete fires even when a step fails."""
    from genblaze_core.pipeline.result import StepCompleteEvent

    events: list[StepCompleteEvent] = []
    failing = FailingProvider()
    (
        Pipeline("fail-callback")
        .step(failing, model="m", prompt="will fail")
        .run(on_step_complete=events.append)
    )

    assert len(events) == 1
    assert events[0].step_index == 0
    assert events[0].step.status == StepStatus.FAILED


def test_pipeline_timeout_and_on_step_complete_together() -> None:
    """pipeline_timeout and on_step_complete work together."""
    from genblaze_core.pipeline.result import StepCompleteEvent

    events: list[StepCompleteEvent] = []
    slow = SlowProvider(delay=0.1)

    with pytest.raises(GenblazeError, match="Pipeline timeout exceeded"):
        (
            Pipeline("both-test")
            .step(slow, model="m", prompt="a")
            .step(slow, model="m", prompt="b")
            .step(slow, model="m", prompt="c")
            .run(pipeline_timeout=0.15, on_step_complete=events.append)
        )

    # At least one step should have completed and fired the callback
    assert len(events) >= 1
    assert events[0].step_index == 0


@pytest.mark.asyncio
async def test_arun_pipeline_timeout_triggers() -> None:
    """pipeline_timeout raises GenblazeError in arun() when time exceeds limit."""
    slow = SlowProvider(delay=0.1)
    with pytest.raises(GenblazeError, match="Pipeline timeout exceeded"):
        await (
            Pipeline("async-timeout", chain=True)
            .step(slow, model="m", prompt="a")
            .step(slow, model="m", prompt="b")
            .step(slow, model="m", prompt="c")
            .arun(pipeline_timeout=0.15)
        )


@pytest.mark.asyncio
async def test_arun_on_step_complete_fires() -> None:
    """on_step_complete fires for each step in arun()."""
    from genblaze_core.pipeline.result import StepCompleteEvent

    events: list[StepCompleteEvent] = []
    provider = MockProvider()
    await (
        Pipeline("async-callback")
        .step(provider, model="m1", prompt="a")
        .step(provider, model="m2", prompt="b")
        .arun(on_step_complete=events.append)
    )

    assert len(events) == 2
    assert {e.step_index for e in events} == {0, 1}
    assert all(e.total_steps == 2 for e in events)


def test_fallback_preserves_chain_inputs() -> None:
    """Fallback steps in chain mode receive the previous step's assets."""
    p1 = ChainableProvider(output_url="https://example.com/step1.png")
    # Second provider: bad-model fails, good-model succeeds
    p2 = ModelErrorProvider(failing_models={"bad-model"})

    result = (
        Pipeline("chain-fallback", chain=True)
        .step(p1, model="m1", prompt="first")
        .step(
            p2,
            model="bad-model",
            prompt="second",
            fallback_models=["good-model"],
        )
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    # The fallback step should have received step1's output as input
    assert "bad-model" in p2.invoked_models
    assert "good-model" in p2.invoked_models


# --- input_from fan-in tests ---


def test_input_from_single_index() -> None:
    """Step 2 gets outputs from step 0 via input_from=0."""
    p0 = ChainableProvider(output_url="https://example.com/step0.png")
    p1 = ChainableProvider(output_url="https://example.com/step1.png")
    p2 = ChainableProvider(output_url="https://example.com/step2.png")

    result = (
        Pipeline("fan-in-single")
        .step(p0, model="m0", prompt="zero")
        .step(p1, model="m1", prompt="one")
        .step(p2, model="m2", prompt="two", input_from=0)
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert len(p2.received_inputs[0]) == 1
    assert p2.received_inputs[0][0].url == "https://example.com/step0.png"


def test_input_from_multiple_indices() -> None:
    """Step 2 gets outputs from both step 0 AND step 1 (AV mux pattern)."""
    p0 = ChainableProvider(output_url="https://example.com/video.mp4")
    p1 = ChainableProvider(output_url="https://example.com/audio.mp3")
    p2 = ChainableProvider(output_url="https://example.com/mixed.mp4")

    result = (
        Pipeline("fan-in-multi")
        .step(p0, model="m0", prompt="video")
        .step(p1, model="m1", prompt="audio")
        .step(p2, model="m2", prompt="mix", input_from=[0, 1])
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert len(p2.received_inputs[0]) == 2
    urls = {a.url for a in p2.received_inputs[0]}
    assert "https://example.com/video.mp4" in urls
    assert "https://example.com/audio.mp3" in urls


def test_input_from_overrides_chain_mode() -> None:
    """In chain=True pipeline, input_from takes precedence over prev step."""
    p0 = ChainableProvider(output_url="https://example.com/step0.png")
    p1 = ChainableProvider(output_url="https://example.com/step1.png")
    p2 = ChainableProvider(output_url="https://example.com/step2.png")

    result = (
        Pipeline("fan-in-override", chain=True)
        .step(p0, model="m0", prompt="zero")
        .step(p1, model="m1", prompt="one")
        .step(p2, model="m2", prompt="two", input_from=[0])
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert len(p2.received_inputs[0]) == 1
    assert p2.received_inputs[0][0].url == "https://example.com/step0.png"


def test_input_from_invalid_index_raises() -> None:
    """Referencing a step index beyond completed steps raises GenblazeError."""
    p0 = ChainableProvider()
    p1 = ChainableProvider()

    with pytest.raises(GenblazeError, match="input_from index 5 is out of range"):
        (
            Pipeline("fan-in-bad")
            .step(p0, model="m0", prompt="zero")
            .step(p1, model="m1", prompt="one", input_from=[5])
            .run()
        )


def test_input_from_none_preserves_existing_behavior() -> None:
    """Chain mode works normally when input_from is not set."""
    p0 = ChainableProvider(output_url="https://example.com/chain0.png")
    p1 = ChainableProvider(output_url="https://example.com/chain1.png")

    result = (
        Pipeline("chain-default", chain=True)
        .step(p0, model="m0", prompt="first")
        .step(p1, model="m1", prompt="second")
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert p0.received_inputs[0] == []
    assert len(p1.received_inputs[0]) == 1
    assert p1.received_inputs[0][0].url == "https://example.com/chain0.png"


@pytest.mark.asyncio
async def test_input_from_arun() -> None:
    """input_from works with async arun() execution."""
    p0 = ChainableProvider(output_url="https://example.com/async0.png")
    p1 = ChainableProvider(output_url="https://example.com/async1.png")
    p2 = ChainableProvider(output_url="https://example.com/async2.png")

    result = await (
        Pipeline("fan-in-async")
        .step(p0, model="m0", prompt="zero")
        .step(p1, model="m1", prompt="one")
        .step(p2, model="m2", prompt="two", input_from=[0, 1])
        .arun()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert len(p2.received_inputs[0]) == 2
    urls = {a.url for a in p2.received_inputs[0]}
    assert "https://example.com/async0.png" in urls
    assert "https://example.com/async1.png" in urls


# --- Chain failure propagation tests (fail_fast=False) ---


def test_chain_fail_fast_false_clears_inputs() -> None:
    """With chain=True and fail_fast=False, a failed step clears prev_assets
    so the next step receives empty inputs instead of stale outputs."""
    p1 = ChainableProvider(output_url="https://example.com/step1.png")
    failing = FailingProvider()
    p3 = ChainableProvider(output_url="https://example.com/step3.png")

    result = (
        Pipeline("chain-fail-noff", chain=True)
        .step(p1, model="m1", prompt="first")
        .step(failing, model="m2", prompt="fails")
        .step(p3, model="m3", prompt="third")
        .run(fail_fast=False)
    )

    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 3
    # Step 1 succeeds with no inputs
    assert p1.received_inputs[0] == []
    # Step 3 should get empty inputs (not step 1's outputs)
    assert p3.received_inputs[0] == []


@pytest.mark.asyncio
async def test_async_chain_fail_fast_false_clears_inputs() -> None:
    """Async chain: failed step clears prev_assets when fail_fast=False."""
    p1 = ChainableProvider(output_url="https://example.com/s1.png")
    failing = FailingProvider()
    p3 = ChainableProvider(output_url="https://example.com/s3.png")

    result = await (
        Pipeline("achain-fail-noff", chain=True)
        .step(p1, model="m1", prompt="first")
        .step(failing, model="m2", prompt="fails")
        .step(p3, model="m3", prompt="third")
        .arun(fail_fast=False)
    )

    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 3
    assert p3.received_inputs[0] == []


# --- Capability validation tests ---


class CapabilityProvider(ChainableProvider):
    """Provider with declared capabilities for validation tests."""

    name = "capable"

    def get_capabilities(self):
        from genblaze_core.models.enums import Modality
        from genblaze_core.providers.base import ProviderCapabilities

        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE],
            supported_inputs=["text"],
            accepts_chain_input=False,
        )


class ChainCapableProvider(ChainableProvider):
    """Provider that accepts chain inputs."""

    name = "chain-capable"

    def get_capabilities(self):
        from genblaze_core.models.enums import Modality
        from genblaze_core.providers.base import ProviderCapabilities

        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
        )


def test_capability_validation_rejects_wrong_modality() -> None:
    """Pipeline rejects steps with unsupported modality at build time."""
    from genblaze_core.models.enums import Modality

    p = CapabilityProvider()
    pipe = Pipeline("cap-test").step(p, model="m", prompt="a", modality=Modality.VIDEO)

    with pytest.raises(GenblazeError, match="modality.*not supported"):
        pipe.run()


def test_capability_validation_rejects_chain_without_accepts() -> None:
    """Pipeline rejects chain mode when downstream provider doesn't accept chain input."""
    p1 = ChainCapableProvider()
    p2 = CapabilityProvider()  # accepts_chain_input=False

    pipe = (
        Pipeline("cap-chain", chain=True)
        .step(p1, model="m1", prompt="a")
        .step(p2, model="m2", prompt="b")
    )

    with pytest.raises(GenblazeError, match="does not accept chain input"):
        pipe.run()


def test_capability_validation_allows_valid_chain() -> None:
    """Pipeline allows chain mode when downstream provider accepts chain input."""
    p1 = ChainCapableProvider(output_url="https://example.com/a.png")
    p2 = ChainCapableProvider(output_url="https://example.com/b.png")

    result = (
        Pipeline("cap-chain-ok", chain=True)
        .step(p1, model="m1", prompt="a")
        .step(p2, model="m2", prompt="b")
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED


def test_capability_validation_skips_none_capabilities() -> None:
    """Providers returning None capabilities skip validation (opt-in)."""
    p1 = MockProvider()  # get_capabilities() returns None
    p2 = MockProvider()

    result = (
        Pipeline("no-caps", chain=True)
        .step(p1, model="m1", prompt="a")
        .step(p2, model="m2", prompt="b")
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED


# --- PipelineResult.error_summary tests ---


def test_error_summary_with_failures() -> None:
    """error_summary() aggregates error messages from failed steps."""
    failing = FailingProvider()
    mock = MockProvider()

    result = (
        Pipeline("err-summary")
        .step(failing, model="m1", prompt="fails")
        .step(mock, model="m2", prompt="ok")
        .run(fail_fast=False)
    )

    summary = result.error_summary()
    assert summary is not None
    assert "Step 0" in summary
    assert "failing" in summary


def test_error_summary_none_on_success() -> None:
    """error_summary() returns None when all steps succeed."""
    result = Pipeline("ok").step(MockProvider(), model="m", prompt="p").run()
    assert result.error_summary() is None


# --- _build_step extracts seed and negative_prompt ---


def test_failed_steps_and_succeeded_steps() -> None:
    """PipelineResult.failed_steps() and succeeded_steps() filter correctly."""
    failing = FailingProvider()
    mock = MockProvider()

    result = (
        Pipeline("filter-test")
        .step(failing, model="m1", prompt="fails")
        .step(mock, model="m2", prompt="ok")
        .run(fail_fast=False)
    )

    assert len(result.failed_steps()) == 1
    assert result.failed_steps()[0].provider == "failing"
    assert len(result.succeeded_steps()) == 1
    assert result.succeeded_steps()[0].provider == "mock"


def test_all_succeeded_returns_empty_failed() -> None:
    """failed_steps() is empty when all steps succeed."""
    result = Pipeline("ok").step(MockProvider(), model="m", prompt="p").run()
    assert result.failed_steps() == []
    assert len(result.succeeded_steps()) == 1


def test_fallback_models_persisted_in_metadata() -> None:
    """fallback_models are persisted in step.metadata for replay."""
    provider = ModelErrorProvider(failing_models={"bad-model"})
    result = (
        Pipeline("fb-meta")
        .step(
            provider,
            model="bad-model",
            prompt="test",
            fallback_models=["good-model"],
        )
        .run()
    )

    assert result.run.status == RunStatus.COMPLETED


def test_input_from_persisted_in_metadata() -> None:
    """input_from is persisted in step.metadata for replay."""
    p0 = ChainableProvider(output_url="https://example.com/s0.png")
    p1 = ChainableProvider(output_url="https://example.com/s1.png")

    result = (
        Pipeline("input-from-meta")
        .step(p0, model="m0", prompt="zero")
        .step(p1, model="m1", prompt="one", input_from=[0])
        .run()
    )

    assert result.run.steps[1].metadata.get("_input_from") == [0]


def test_metadata_with_fallback_and_input_from() -> None:
    """Both _fallback_models and _input_from can be in metadata together."""
    p0 = ChainableProvider(output_url="https://example.com/s0.png")
    p1 = ModelErrorProvider(failing_models=set())

    result = (
        Pipeline("combo-meta")
        .step(p0, model="m0", prompt="zero")
        .step(p1, model="m1", prompt="one", fallback_models=["m2"], input_from=[0])
        .run()
    )

    meta = result.run.steps[1].metadata
    assert meta.get("_fallback_models") == ["m2"]
    assert meta.get("_input_from") == [0]


def test_on_submit_callback_fires() -> None:
    """on_submit callback receives step_id and prediction_id."""
    from genblaze_core.runnable.config import RunnableConfig

    submissions: list[tuple[str, str]] = []

    def on_submit(step_id, prediction_id):
        submissions.append((step_id, prediction_id))

    provider = MockProvider()
    config = RunnableConfig(timeout=30, max_retries=0)
    config["on_submit"] = on_submit

    pipe = Pipeline("submit-test").step(provider, model="m", prompt="p")
    result = pipe.run(_config_override=config)
    assert result.run.status == RunStatus.COMPLETED
    assert len(submissions) == 1
    assert submissions[0][1] == "pred-123"  # MockProvider returns "pred-123"


@pytest.mark.asyncio
async def test_on_submit_callback_fires_async() -> None:
    """on_submit callback fires in async path too."""
    from genblaze_core.runnable.config import RunnableConfig

    submissions: list[tuple[str, str]] = []

    def on_submit(step_id, prediction_id):
        submissions.append((step_id, prediction_id))

    provider = MockProvider()
    config = RunnableConfig(timeout=30, max_retries=0)
    config["on_submit"] = on_submit

    pipe = Pipeline("async-submit").step(provider, model="m", prompt="p")
    result = await pipe.arun(_config_override=config)
    assert result.run.status == RunStatus.COMPLETED
    assert len(submissions) == 1


def test_build_step_extracts_seed_and_negative_prompt() -> None:
    """Pipeline.step() kwargs for seed/negative_prompt go to Step fields, not params."""
    p = ChainableProvider()
    result = (
        Pipeline("seed-test")
        .step(p, model="m", prompt="p", seed=42, negative_prompt="blurry")
        .run()
    )

    step = result.run.steps[0]
    assert step.seed == 42
    assert step.negative_prompt == "blurry"
    assert "seed" not in step.params
    assert "negative_prompt" not in step.params
