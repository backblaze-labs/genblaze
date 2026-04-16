"""Tests for Parquet sink."""

from pathlib import Path

import pyarrow.parquet as pq
from genblaze_core.models import Manifest, Run, Step
from genblaze_core.models.asset import Asset
from genblaze_core.sinks.parquet import ParquetSink


def test_write_run_split_tables(tmp_path: Path):
    """Verify runs/steps/assets tables are created."""
    step = Step(provider="replicate", model="flux-schnell", prompt="test")
    step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
    run = Run(steps=[step], tenant_id="acme")
    manifest = Manifest(run=run)
    manifest.compute_hash()

    sink = ParquetSink(tmp_path / "parquet_out")
    sink.write_run(run, manifest)

    # runs table
    run_files = list((tmp_path / "parquet_out" / "runs").rglob("*.parquet"))
    assert len(run_files) == 1
    run_table = pq.ParquetFile(run_files[0]).read()
    assert run_table.num_rows == 1
    assert run_table.column("run_id")[0].as_py() == run.run_id
    assert run_table.column("canonical_hash")[0].as_py() == manifest.canonical_hash

    # steps table
    step_files = list((tmp_path / "parquet_out" / "steps").rglob("*.parquet"))
    assert len(step_files) == 1
    step_table = pq.ParquetFile(step_files[0]).read()
    assert step_table.num_rows == 1
    assert step_table.column("step_id")[0].as_py() == step.step_id

    # assets table
    asset_files = list((tmp_path / "parquet_out" / "assets").rglob("*.parquet"))
    assert len(asset_files) == 1
    asset_table = pq.ParquetFile(asset_files[0]).read()
    assert asset_table.num_rows == 1
    assert asset_table.column("url")[0].as_py() == "https://example.com/out.png"


def test_write_run_default_tenant(tmp_path: Path):
    step = Step(provider="test", model="m")
    run = Run(steps=[step])
    manifest = Manifest(run=run)
    manifest.compute_hash()

    sink = ParquetSink(tmp_path / "out")
    sink.write_run(run, manifest)

    parquet_files = list((tmp_path / "out" / "runs").rglob("*.parquet"))
    assert len(parquet_files) == 1
    assert "tenant_id=default" in str(parquet_files[0])


def test_parent_run_id_in_runs_table(tmp_path: Path):
    """parent_run_id is written to the Parquet runs table for lineage queries."""
    step = Step(provider="test", model="m")
    run = Run(steps=[step], parent_run_id="parent-abc-123")
    manifest = Manifest(run=run)
    manifest.compute_hash()

    sink = ParquetSink(tmp_path / "out")
    sink.write_run(run, manifest)

    run_files = list((tmp_path / "out" / "runs").rglob("*.parquet"))
    run_table = pq.ParquetFile(run_files[0]).read()
    assert run_table.column("parent_run_id")[0].as_py() == "parent-abc-123"


def test_parent_run_id_null_when_unset(tmp_path: Path):
    """parent_run_id is null in Parquet when not set on the run."""
    step = Step(provider="test", model="m")
    run = Run(steps=[step])
    manifest = Manifest(run=run)
    manifest.compute_hash()

    sink = ParquetSink(tmp_path / "out")
    sink.write_run(run, manifest)

    run_files = list((tmp_path / "out" / "runs").rglob("*.parquet"))
    run_table = pq.ParquetFile(run_files[0]).read()
    assert run_table.column("parent_run_id")[0].as_py() is None


def test_idempotent_write(tmp_path: Path):
    """Writing the same run twice should not duplicate data."""
    step = Step(provider="test", model="m")
    run = Run(steps=[step])
    manifest = Manifest(run=run)
    manifest.compute_hash()

    sink = ParquetSink(tmp_path / "out")
    sink.write_run(run, manifest)
    sink.write_run(run, manifest)  # second write should be skipped

    parquet_files = list((tmp_path / "out" / "runs").rglob("*.parquet"))
    assert len(parquet_files) == 1
