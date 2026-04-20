"""ObjectStorageSink — upload assets and manifests to object storage."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from genblaze_core.exceptions import SinkError
from genblaze_core.sinks.base import BaseSink
from genblaze_core.storage.base import KeyStrategy
from genblaze_core.storage.transfer import AssetTransfer

if TYPE_CHECKING:
    from genblaze_core.models.manifest import Manifest
    from genblaze_core.models.run import Run
    from genblaze_core.sinks.parquet import ParquetSink
    from genblaze_core.storage.base import StorageBackend

logger = logging.getLogger("genblaze.storage.sink")

# Max parallel asset uploads within a single write_run call
_DEFAULT_UPLOAD_WORKERS = 4


class ObjectStorageSink(BaseSink):
    """Upload run assets and manifests to an object storage backend.

    Optionally delegates to a ParquetSink for structured data, and can
    upload the resulting Parquet files to storage as well.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        prefix: str = "genblaze",
        key_strategy: KeyStrategy = KeyStrategy.CONTENT_ADDRESSABLE,
        parquet_sink: ParquetSink | None = None,
        max_upload_workers: int = _DEFAULT_UPLOAD_WORKERS,
    ):
        self._backend = backend
        self._prefix = prefix
        self._key_strategy = key_strategy
        is_hierarchical = key_strategy == KeyStrategy.HIERARCHICAL
        asset_prefix = f"{prefix}/runs" if is_hierarchical else f"{prefix}/assets"
        self._transfer = AssetTransfer(backend, prefix=asset_prefix, key_strategy=key_strategy)
        self._parquet_sink = parquet_sink
        self._max_upload_workers = max_upload_workers
        # Lock protects only the manifest write (check-then-put must be atomic)
        self._manifest_lock = threading.Lock()

    def write_run(self, run: Run, manifest: Manifest) -> None:
        try:
            self._write_run_impl(run, manifest)
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"ObjectStorageSink failed: {exc}") from exc

    def _build_manifest_key(self, run: Run) -> str:
        """Build the storage key for the manifest based on key strategy."""
        if self._key_strategy == KeyStrategy.HIERARCHICAL:
            parts = [self._prefix, "runs"]
            if run.tenant_id:
                parts.append(run.tenant_id)
            parts.append(run.created_at.strftime("%Y-%m-%d"))
            parts.append(run.run_id)
            parts.append("manifest.json")
            return "/".join(parts)
        return f"{self._prefix}/manifests/{run.run_id}.json"

    def _write_run_impl(self, run: Run, manifest: Manifest) -> None:
        date_str = run.created_at.strftime("%Y-%m-%d")

        # 1. Transfer assets in parallel (no lock needed — idempotent)
        failed_asset_ids: list[str] = []
        all_assets = [(step, asset) for step in run.steps for asset in step.assets]
        if all_assets:
            workers = min(self._max_upload_workers, len(all_assets))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._transfer.transfer,
                        asset,
                        tenant=run.tenant_id,
                        date_str=date_str,
                        run_id=run.run_id,
                    ): asset
                    for _step, asset in all_assets
                }
                for future in as_completed(futures):
                    asset = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        failed_asset_ids.append(asset.asset_id)
                        logger.warning("Asset transfer failed for %s: %s", asset.asset_id, exc)

        if failed_asset_ids:
            logger.warning(
                "Run %s: %d/%d asset transfers failed: %s",
                run.run_id,
                len(failed_asset_ids),
                len(all_assets),
                ", ".join(failed_asset_ids),
            )

        # 2. Record partial failures on the manifest as transport-layer
        # diagnostics — NOT on run.metadata, which is part of the hashed
        # payload (see manifest._RUN_HASH_EXCLUDE). transfer_failures is a
        # non-hashed Manifest field, so writing it here doesn't affect hash
        # integrity and verify() still succeeds after partial failures.
        if failed_asset_ids:
            manifest.transfer_failures = list(failed_asset_ids)

        # 3. Recompute manifest hash after asset transfers mutated URLs/hashes
        manifest.compute_hash()

        # 4. Upload manifest JSON (lock protects check-then-put atomicity)
        manifest_key = self._build_manifest_key(run)
        with self._manifest_lock:
            if not self._backend.exists(manifest_key):
                manifest_json = manifest.to_canonical_json()
                self._backend.put(
                    manifest_key,
                    manifest_json.encode("utf-8"),
                    content_type="application/json",
                )
                manifest.manifest_uri = self._backend.get_url(manifest_key)
                logger.info("Manifest uploaded: %s", manifest_key)

        # 5. Optionally write to ParquetSink
        if self._parquet_sink is not None:
            self._parquet_sink.write_run(run, manifest)

    def close(self) -> None:
        """Release storage backend resources."""
        self._backend.close()
        if self._parquet_sink is not None:
            self._parquet_sink.close()
