"""Tests for Parquet sink."""

from pathlib import Path

import pyarrow.parquet as pq
from genblaze_core.models import EmbedPolicy, Manifest, Modality, PromptVisibility, Run, Step
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


def test_idempotent_write_when_content_changes_moves_partition(tmp_path: Path):
    """Re-sinking a run_id whose content changed (moving the content-derived
    partition) must not create duplicate runs/steps/assets rows (#72).

    This is the normal resume/CLI-replay shape: a first partial sink, then a
    later sink once more steps complete. The partition is derived from the
    step modality/provider set, so it moves between the two writes even
    though run_id — the documented idempotency key — is unchanged.
    """
    step1 = Step(provider="replicate", model="m", modality=Modality.IMAGE)
    step1.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
    run = Run(steps=[step1])
    manifest = Manifest(run=run)
    manifest.compute_hash()

    sink = ParquetSink(tmp_path / "out")
    sink.write_run(run, manifest)
    stale_runs_files = list((tmp_path / "out" / "runs").rglob("*.parquet"))
    assert len(stale_runs_files) == 1

    # Same run_id gains a second step with a different modality/provider —
    # the content-derived partition path for this run_id now differs.
    step2 = Step(provider="elevenlabs", model="m2", modality=Modality.AUDIO)
    step2.assets.append(Asset(url="https://example.com/out.mp3", media_type="audio/mp3"))
    run.steps.append(step2)
    manifest2 = Manifest(run=run)
    manifest2.compute_hash()
    sink.write_run(run, manifest2)

    runs_files = list((tmp_path / "out" / "runs").rglob("*.parquet"))
    steps_files = list((tmp_path / "out" / "steps").rglob("*.parquet"))
    assets_files = list((tmp_path / "out" / "assets").rglob("*.parquet"))

    # Exactly one runs sentinel for this run_id — the stale one under the
    # old partition was removed, not left alongside a new duplicate.
    assert len(runs_files) == 1
    assert runs_files != stale_runs_files
    run_table = pq.ParquetFile(runs_files[0]).read()
    assert run_table.num_rows == 1
    assert run_table.column("run_id")[0].as_py() == run.run_id
    assert run_table.column("step_count")[0].as_py() == 2

    # steps/assets tables reflect the latest write only — no duplicates
    # accumulated from the first (now-stale) partial write.
    assert len(steps_files) == 1
    assert pq.ParquetFile(steps_files[0]).read().num_rows == 2
    assert len(assets_files) == 1
    assert pq.ParquetFile(assets_files[0]).read().num_rows == 2


def test_new_run_write_does_not_scan_unrelated_partitions(tmp_path: Path, monkeypatch):
    """A brand-new run_id must resolve via the run_id -> partition index (a
    single file stat) rather than a full-tree glob over runs/, even once the
    tree already holds many unrelated partitions (#150)."""
    sink = ParquetSink(tmp_path / "out")

    # Populate several unrelated partitions (distinct run_ids/tenants).
    for i in range(5):
        step = Step(provider="test", model="m")
        run = Run(steps=[step], tenant_id=f"tenant-{i}")
        manifest = Manifest(run=run)
        manifest.compute_hash()
        sink.write_run(run, manifest)

    glob_calls: list[str] = []
    original_glob = Path.glob

    def spy_glob(self, pattern, *args, **kwargs):
        glob_calls.append(pattern)
        return original_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", spy_glob)

    # A genuinely new run_id — must never trigger a full-tree glob.
    new_step = Step(provider="test", model="m")
    new_run = Run(steps=[new_step], tenant_id="tenant-new")
    new_manifest = Manifest(run=new_run)
    new_manifest.compute_hash()
    sink.write_run(new_run, new_manifest)

    assert glob_calls == []


def test_write_run_applies_embed_policy_redaction(tmp_path: Path):
    """ParquetSink redacts prompt, params, and seed when policy requires it."""
    step = Step(
        provider="test",
        model="m",
        prompt="SECRET PROMPT",
        seed=12345,
        params={"public": "ok", "secret": "do-not-store"},
    )
    run = Run(steps=[step])
    manifest = Manifest(run=run)
    manifest.compute_hash()
    policy = EmbedPolicy(
        prompt_visibility=PromptVisibility.PRIVATE,
        include_params=False,
        include_seed=False,
    )

    sink = ParquetSink(tmp_path / "out", policy=policy)
    sink.write_run(run, manifest)

    step_files = list((tmp_path / "out" / "steps").rglob("*.parquet"))
    assert len(step_files) == 1
    step_table = pq.ParquetFile(step_files[0]).read()
    assert step_table.column("prompt")[0].as_py() == ""
    assert step_table.column("seed")[0].as_py() is None
    assert step_table.column("params_json")[0].as_py() == "{}"
