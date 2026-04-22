"""Integration tests for chained pipelines, sinks, and capability validation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, RunStatus, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities
from genblaze_core.runnable.config import RunnableConfig


class TrackingProvider(BaseProvider):
    """Provider that tracks inputs and produces predictable outputs."""

    name = "tracking"

    def __init__(
        self,
        output_url: str = "https://example.com/out.png",
        media_type: str = "image/png",
    ):
        super().__init__()
        self._output_url = output_url
        self._media_type = media_type
        self.received_inputs: list[list[Asset]] = []

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.IMAGE, Modality.VIDEO],
            supported_inputs=["text", "image"],
            accepts_chain_input=True,
        )

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.received_inputs.append(list(step.inputs))
        return "pred-tracking"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url=self._output_url, media_type=self._media_type))
        return step


class FailingProvider(BaseProvider):
    """Provider that always fails."""

    name = "failing"

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        raise RuntimeError("Provider failed")

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        return step


def test_chain_pipeline_to_parquet_sink(tmp_path) -> None:
    """Chained pipeline writes step inputs to Parquet sink correctly."""
    from genblaze_core.sinks.parquet import ParquetSink

    sink = ParquetSink(tmp_path / "parquet")
    p1 = TrackingProvider(output_url="https://example.com/img.png")
    p2 = TrackingProvider(output_url="https://example.com/vid.mp4", media_type="video/mp4")

    result = (
        Pipeline("chain-parquet", chain=True)
        .step(p1, model="gen", prompt="generate image")
        .step(p2, model="animate", prompt="animate it", modality=Modality.VIDEO)
        .run(sink=sink)
    )

    assert result.run.status == RunStatus.COMPLETED

    # Verify Parquet files were written
    import pyarrow.parquet as pq

    runs_files = list((tmp_path / "parquet" / "runs").rglob("*.parquet"))
    steps_files = list((tmp_path / "parquet" / "steps").rglob("*.parquet"))
    assets_files = list((tmp_path / "parquet" / "assets").rglob("*.parquet"))

    assert len(runs_files) == 1
    assert len(steps_files) == 1
    assert len(assets_files) == 1

    # Read individual files directly to avoid Hive partition type conflicts
    runs_table = pq.ParquetFile(runs_files[0]).read()
    assert runs_table.num_rows == 1
    assert runs_table.column("status")[0].as_py() == "completed"

    # Verify steps data
    steps_table = pq.ParquetFile(steps_files[0]).read()
    assert steps_table.num_rows == 2


def test_chain_pipeline_to_object_storage_sink() -> None:
    """Chained pipeline uploads assets and manifest to mocked object storage."""
    from genblaze_core.storage.base import KeyStrategy, StorageBackend
    from genblaze_core.storage.sink import ObjectStorageSink

    # Mock backend
    backend = MagicMock(spec=StorageBackend)
    backend.exists.return_value = False
    backend.get_url.return_value = "https://bucket.example.com/manifest.json"
    # Sink persists the durable URL into manifest.manifest_uri; Pydantic
    # rejects the raw MagicMock return without this stub.
    backend.get_durable_url.return_value = "https://bucket.example.com/manifest.json"

    sink = ObjectStorageSink(backend, key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)

    p1 = TrackingProvider(output_url="https://example.com/img.png")
    p2 = TrackingProvider(output_url="https://example.com/vid.mp4", media_type="video/mp4")

    result = (
        Pipeline("chain-storage", chain=True)
        .step(p1, model="gen", prompt="image")
        .step(p2, model="anim", prompt="animate", modality=Modality.VIDEO)
        .run(sink=sink)
    )

    assert result.run.status == RunStatus.COMPLETED
    # Backend should have been called for manifest upload
    assert backend.put.called


def test_chain_fail_fast_false_with_recovery() -> None:
    """Chain with fail_fast=False: step after failure gets empty inputs,
    but step after a subsequent success gets that success's outputs."""
    p1 = TrackingProvider(output_url="https://example.com/s1.png")
    failing = FailingProvider()
    p3 = TrackingProvider(output_url="https://example.com/s3.png")
    p4 = TrackingProvider(output_url="https://example.com/s4.png")

    result = (
        Pipeline("chain-recover", chain=True)
        .step(p1, model="m1", prompt="ok")
        .step(failing, model="m2", prompt="fails")
        .step(p3, model="m3", prompt="after fail")
        .step(p4, model="m4", prompt="after recovery")
        .run(fail_fast=False)
    )

    assert result.run.status == RunStatus.FAILED
    assert len(result.run.steps) == 4

    # p3 gets empty inputs (failure cleared prev_assets)
    assert p3.received_inputs[0] == []
    # p4 gets p3's outputs (p3 succeeded)
    assert len(p4.received_inputs[0]) == 1
    assert p4.received_inputs[0][0].url == "https://example.com/s3.png"


def test_batch_run_with_chain_mode() -> None:
    """batch_run with chain=True produces independent runs per prompt."""
    p1 = TrackingProvider(output_url="https://example.com/img.png")
    p2 = TrackingProvider(output_url="https://example.com/vid.mp4", media_type="video/mp4")

    results = (
        Pipeline("batch-chain", chain=True)
        .step(p1, model="gen", prompt="placeholder")
        .step(p2, model="anim", prompt="placeholder", modality=Modality.VIDEO)
        .batch_run(["prompt A", "prompt B"])
    )

    assert len(results) == 2
    assert all(r.run.status == RunStatus.COMPLETED for r in results)
    # Each batch item should have its own run_id
    assert results[0].run.run_id != results[1].run.run_id


def test_error_summary_includes_transfer_failures() -> None:
    """PipelineResult.error_summary includes transfer failure diagnostics."""
    from genblaze_core.models.manifest import Manifest
    from genblaze_core.models.run import Run
    from genblaze_core.pipeline.result import PipelineResult

    step = Step(provider="test", model="m", prompt="p", status=StepStatus.SUCCEEDED)
    run = Run(steps=[step])
    manifest = Manifest.from_run(run)
    manifest.transfer_failures = ["asset-abc", "asset-def"]
    result = PipelineResult(run, manifest)

    summary = result.error_summary()
    assert summary is not None
    assert "asset-abc" in summary
    assert "asset-def" in summary
