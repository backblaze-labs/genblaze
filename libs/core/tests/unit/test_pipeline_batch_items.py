"""Coverage for ``Pipeline.batch_run(items=...)`` per-item param fan-out."""

from __future__ import annotations

import asyncio

import pytest
from genblaze_core.models.enums import Modality
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.testing import MockProvider


def test_items_overload_merges_into_step0_params() -> None:
    provider = MockProvider()
    pipe = Pipeline("test").step(
        provider, model="m", prompt="default", modality=Modality.IMAGE, seed=1
    )
    items = [
        {"prompt": "a cat", "seed": 7, "aspect_ratio": "16:9"},
        {"prompt": "a dog", "seed": 8, "aspect_ratio": "9:16"},
    ]
    results = pipe.batch_run(items=items, raise_on_failure=False)
    assert len(results) == 2
    # ``seed`` is promoted to a top-level Step field by ``_build_step``.
    received = sorted(provider.received_steps, key=lambda s: s.seed or 0)
    assert received[0].prompt == "a cat"
    assert received[0].seed == 7
    assert received[0].params["aspect_ratio"] == "16:9"
    assert received[1].prompt == "a dog"
    assert received[1].seed == 8


def test_items_does_not_mutate_original_steps() -> None:
    provider = MockProvider()
    pipe = Pipeline("test").step(
        provider, model="m", prompt="kept", modality=Modality.IMAGE, seed=42
    )
    pipe.batch_run(items=[{"prompt": "override", "seed": 99}], raise_on_failure=False)
    # Original pipeline unchanged — important for reusable pipelines.
    assert pipe._steps[0].prompt == "kept"
    assert pipe._steps[0].params["seed"] == 42


def test_items_and_prompts_are_mutually_exclusive() -> None:
    pipe = Pipeline("test").step(MockProvider(), model="m", modality=Modality.IMAGE)
    with pytest.raises(ValueError, match="not both"):
        pipe.batch_run(prompts=["x"], items=[{"prompt": "y"}])


def test_batch_requires_one_of_prompts_or_items() -> None:
    pipe = Pipeline("test").step(MockProvider(), model="m", modality=Modality.IMAGE)
    with pytest.raises(ValueError, match="requires either"):
        pipe.batch_run()


def test_async_items_overload() -> None:
    provider = MockProvider()
    pipe = Pipeline("test").step(provider, model="m", modality=Modality.IMAGE)
    results = asyncio.run(
        pipe.abatch_run(
            items=[{"prompt": "alpha", "seed": 1}, {"prompt": "beta", "seed": 2}],
            raise_on_failure=False,
        )
    )
    assert len(results) == 2
    seeds = sorted(s.seed for s in provider.received_steps if s.seed is not None)
    assert seeds == [1, 2]
