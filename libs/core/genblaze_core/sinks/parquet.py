"""Parquet sink — write run events to partitioned Parquet files.

Requires the ``parquet`` extra: ``pip install "genblaze-core[parquet]"``
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as _exc:
    from genblaze_core._optional import OptionalDependencyError

    raise OptionalDependencyError(
        extra="parquet",
        package="pyarrow",
        symbol="ParquetSink",
    ) from _exc

import re

from genblaze_core.canonical.json import canonical_json
from genblaze_core.exceptions import SinkError
from genblaze_core.models.enums import PromptVisibility
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.policy import EmbedPolicy
from genblaze_core.models.run import Run
from genblaze_core.sinks.base import BaseSink

# Only allow safe characters in partition path components
_SAFE_PARTITION = re.compile(r"[^A-Za-z0-9_\-.]")

# run_id -> partition index, one small file per run_id under this directory
# (a sibling of runs/steps/assets, not inside them, so Hive-style dataset
# readers scanning those trees never see it). Sharding by run_id — rather
# than one shared index file — means concurrent writers sinking *different*
# run_ids into the same base_dir (e.g. parallel `genblaze index` invocations)
# never race on the same file (#150).
_INDEX_DIRNAME = "_run_index"


def _atomic_write_table(table: pa.Table, dest: Path) -> None:
    """Write a Parquet table atomically via temp file + os.replace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_bytes(data: bytes, dest: Path) -> None:
    """Write bytes atomically via temp file + os.replace (mirrors _atomic_write_table)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ParquetSink(BaseSink):
    """Write run data to Parquet files partitioned by date/tenant/modality/provider.

    Produces three tables:
    - runs/{partition}/run_id.parquet
    - steps/{partition}/run_id.parquet
    - assets/{partition}/run_id.parquet
    """

    def __init__(self, base_dir: str | Path, *, policy: EmbedPolicy | None = None):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._policy = policy
        self._lock = threading.Lock()
        self._index_dir = self.base_dir / _INDEX_DIRNAME
        self._bootstrap_run_index()

    def _sanitize(self, value: str) -> str:
        """Sanitize a string for use in partition paths (prevent traversal)."""
        return _SAFE_PARTITION.sub("_", value)

    def _bootstrap_run_index(self) -> None:
        """One-time backfill of the run_id -> partition index from an
        existing ``runs/`` tree (e.g. the first use after upgrading past
        #150). The index directory's mere existence marks this done, so a
        brand-new or already-bootstrapped ``base_dir`` costs a single
        directory stat and never pays for a full-tree glob again.
        """
        if self._index_dir.exists():
            return
        runs_dir = self.base_dir / "runs"
        if runs_dir.exists():
            for sentinel in runs_dir.glob("**/*.parquet"):
                partition = sentinel.relative_to(runs_dir).parent.as_posix()
                self._write_run_index_entry(sentinel.stem, partition)
        self._index_dir.mkdir(parents=True, exist_ok=True)

    def _run_index_entry_path(self, run_id: str) -> Path:
        return self._index_dir / f"{run_id}.partition"

    def _lookup_run_partition(self, run_id: str) -> str | None:
        """Return the partition this run_id was last sunk under, or None if unknown."""
        try:
            return self._run_index_entry_path(run_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _write_run_index_entry(self, run_id: str, partition: str) -> None:
        """Persist run_id -> partition as its own small file (see _INDEX_DIRNAME)."""
        _atomic_write_bytes(partition.encode("utf-8"), self._run_index_entry_path(run_id))

    def _remove_partition_files(self, run_id: str, partition: str) -> None:
        """Remove a stale run's steps/assets/runs files under `partition`.

        Steps/assets removed before runs (mirroring "runs is written last" on
        the write side) so an interrupted cleanup is safely retryable: if a
        crash lands mid-cleanup, the stale runs sentinel is still present, a
        retry's probe finds it again, and the whole stale partition is
        cleaned from scratch — never leaving orphaned steps/assets with no
        sentinel pointing at them.
        """
        for table in ("steps", "assets", "runs"):
            (self.base_dir / table / partition / f"{run_id}.parquet").unlink(missing_ok=True)

    def _partition_path(self, run: Run) -> str:
        date_str = run.created_at.strftime("%Y-%m-%d")
        tenant = self._sanitize(run.tenant_id or "default")
        modalities = {str(s.modality) for s in run.steps}
        modality_str = self._sanitize("_".join(sorted(modalities)) or "unknown")
        # Filter None — INGEST/IMPORT steps have no upstream provider; the
        # partition path replaces them with the modality-default sentinel
        # rather than the literal string "None".
        providers = {s.provider for s in run.steps if s.provider is not None}
        provider_str = self._sanitize("_".join(sorted(providers)) or "unknown")
        return f"dt={date_str}/tenant_id={tenant}/modality={modality_str}/provider={provider_str}"

    def write_run(self, run: Run, manifest: Manifest) -> None:
        try:
            with self._lock:
                self._write_run_impl(run, manifest)
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Failed to write Parquet: {exc}") from exc

    def _write_run_impl(self, run: Run, manifest: Manifest) -> None:
        partition = self._partition_path(run)

        # Idempotency: runs table is written last as a completion sentinel.
        # Fast path first: an exact-path match (unchanged content, including
        # every ordinary first-time write) is a pure no-op re-write and the
        # overwhelmingly common case, so check it with a single stat() before
        # paying for anything more expensive.
        runs_path = self.base_dir / "runs" / partition / f"{run.run_id}.parquet"
        if runs_path.exists():
            return

        # The partition is derived from run *content* (step modality/provider
        # set), which can change between sinks of the same run_id — e.g. a
        # resume that completes more steps, or a CLI replay re-indexing a
        # richer manifest. The fast-path check above only covers the current
        # partition, so it misses a sentinel written earlier under a
        # *different* partition, letting a second `runs` row and duplicate
        # `steps`/`assets` rows accumulate for one run_id (#72). Only pay for
        # a lookup once the fast path misses (new run_id or a moved
        # partition): find any prior sentinel via the persisted run_id ->
        # partition index instead of globbing the whole tree on every write
        # (#150) — a genuinely new run_id has no index entry and costs a
        # single file stat.
        stale_partition = self._lookup_run_partition(run.run_id)
        if stale_partition is not None and stale_partition != partition:
            self._remove_partition_files(run.run_id, stale_partition)

        # --- steps table (written before runs) ---
        step_rows = []
        for step in run.steps:
            # Apply EmbedPolicy redaction if configured
            prompt = step.prompt or ""
            seed = step.seed
            params_json = canonical_json(step.params)
            if self._policy:
                if self._policy.prompt_visibility == PromptVisibility.PRIVATE:
                    prompt = ""
                if not self._policy.include_params:
                    params_json = "{}"
                if not self._policy.include_seed:
                    seed = None

            step_rows.append(
                {
                    "run_id": run.run_id,
                    "step_id": step.step_id,
                    "provider": step.provider,
                    "model": step.model,
                    "step_type": str(step.step_type),
                    "modality": str(step.modality),
                    "status": str(step.status),
                    "prompt": prompt,
                    "seed": seed,
                    "params_json": params_json,
                    "asset_count": len(step.assets),
                    "retries": step.retries,
                    "cost_usd": step.cost_usd,
                    "error": step.error,
                    "error_code": str(step.error_code) if step.error_code else None,
                    "started_at": step.started_at.isoformat() if step.started_at else "",
                    "completed_at": step.completed_at.isoformat() if step.completed_at else "",
                }
            )
        if step_rows:
            _atomic_write_table(
                pa.Table.from_pylist(step_rows),
                self.base_dir / "steps" / partition / f"{run.run_id}.parquet",
            )

        # --- assets table (written before runs) ---
        asset_rows = []
        for step in run.steps:
            for asset in step.assets:
                row = {
                    "run_id": run.run_id,
                    "step_id": step.step_id,
                    "asset_id": asset.asset_id,
                    "url": asset.url,
                    "media_type": asset.media_type,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                    "width": asset.width,
                    "height": asset.height,
                    "duration": asset.duration,
                    # Video metadata
                    "frame_rate": asset.video.frame_rate if asset.video else None,
                    "video_codec": asset.video.codec if asset.video else None,
                    "video_bitrate": asset.video.bitrate if asset.video else None,
                    "color_space": asset.video.color_space if asset.video else None,
                    "has_audio": asset.video.has_audio if asset.video else None,
                    # Audio metadata
                    "sample_rate": asset.audio.sample_rate if asset.audio else None,
                    "channels": asset.audio.channels if asset.audio else None,
                    "audio_codec": asset.audio.codec if asset.audio else None,
                    # Track count for multi-stream containers
                    "track_count": len(asset.tracks) if asset.tracks else None,
                }
                asset_rows.append(row)
        if asset_rows:
            _atomic_write_table(
                pa.Table.from_pylist(asset_rows),
                self.base_dir / "assets" / partition / f"{run.run_id}.parquet",
            )

        # --- runs table (written last as completion sentinel, atomic) ---
        run_row = {
            "run_id": run.run_id,
            "parent_run_id": run.parent_run_id,
            "tenant_id": run.tenant_id or "default",
            "project_id": run.project_id,
            "name": run.name or "",
            "status": str(run.status),
            "step_count": len(run.steps),
            "canonical_hash": manifest.canonical_hash,
            "created_at": run.created_at.isoformat(),
        }
        _atomic_write_table(pa.Table.from_pylist([run_row]), runs_path)

        # Record the partition this run_id now lives at *after* the sentinel
        # write succeeds, so the index only ever reflects confirmed on-disk
        # state. As with the stale-partition cleanup above, this is not a
        # cross-file transaction: a crash between the sentinel write and this
        # persist leaves this run_id's index entry stale (pointing at the
        # *previous* partition) — the same accepted trade-off already
        # documented for the sentinel-cleanup step, and just as narrow: the
        # run's own data is fully and correctly sunk either way.
        self._write_run_index_entry(run.run_id, partition)

    def close(self) -> None:
        pass
