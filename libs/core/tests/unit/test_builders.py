"""Tests for fluent builders."""

from genblaze_core.builders import RunBuilder, StepBuilder
from genblaze_core.models.enums import Modality
from genblaze_core.models.manifest import Manifest


def test_step_builder():
    step = (
        StepBuilder("replicate", "flux-schnell")
        .prompt("a cat in space")
        .modality(Modality.IMAGE)
        .params(width=1024, height=1024)
        .build()
    )
    assert step.provider == "replicate"
    assert step.model == "flux-schnell"
    assert step.prompt == "a cat in space"
    assert step.params["width"] == 1024


def test_run_builder():
    step = StepBuilder("replicate", "flux-schnell").prompt("test").build()
    run = RunBuilder("my-run").tenant("acme").add_step(step).build()
    assert run.name == "my-run"
    assert run.tenant_id == "acme"
    assert len(run.steps) == 1
    assert run.steps[0].run_id == run.run_id


def test_manifest_from_run():
    """Manifest.from_run() creates a hashed, verifiable manifest."""
    step = StepBuilder("replicate", "flux-schnell").prompt("test").build()
    run = RunBuilder().add_step(step).build()
    manifest = Manifest.from_run(run)
    assert manifest.canonical_hash
    assert manifest.verify()


def test_run_builder_parent():
    """RunBuilder.parent() sets parent_run_id for iteration lineage."""
    step = StepBuilder("replicate", "flux-schnell").prompt("test").build()
    run = RunBuilder("v2").parent("parent-run-abc").add_step(step).build()
    assert run.parent_run_id == "parent-run-abc"

    # parent_run_id is excluded from canonical hash
    manifest = Manifest.from_run(run)
    original_hash = manifest.canonical_hash
    run.parent_run_id = "different-parent"
    manifest2 = Manifest.from_run(run)
    assert manifest2.canonical_hash == original_hash


def test_run_builder_double_build_does_not_mutate():
    """Calling build() twice should not corrupt the first run's steps."""
    step = StepBuilder("replicate", "flux-schnell").prompt("test").build()
    rb = RunBuilder("double").add_step(step)

    run1 = rb.build()
    run2 = rb.build()

    # Each run gets its own run_id
    assert run1.run_id != run2.run_id
    # Steps in each run point to their own run
    assert run1.steps[0].run_id == run1.run_id
    assert run2.steps[0].run_id == run2.run_id
    # Original step is not mutated
    assert step.run_id is None
