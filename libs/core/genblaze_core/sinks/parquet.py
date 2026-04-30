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
    raise ImportError(
        "pyarrow is required for ParquetSink. "
        'Install it with: pip install "genblaze-core[parquet]"'
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

    def _sanitize(self, value: str) -> str:
        """Sanitize a string for use in partition paths (prevent traversal)."""
        return _SAFE_PARTITION.sub("_", value)

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
        # If it exists, all tables are already written.
        runs_path = self.base_dir / "runs" / partition / f"{run.run_id}.parquet"
        if runs_path.exists():
            return

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

    def close(self) -> None:
        pass
