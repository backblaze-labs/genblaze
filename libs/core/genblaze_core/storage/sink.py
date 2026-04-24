"""ObjectStorageSink — upload assets and manifests to object storage."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import wait as _futures_wait
from typing import TYPE_CHECKING

from genblaze_core.exceptions import SinkError
from genblaze_core.models.enums import StepStatus
from genblaze_core.sinks.base import BaseSink
from genblaze_core.storage.base import KeyStrategy
from genblaze_core.storage.transfer import AssetTransfer

if TYPE_CHECKING:
    from genblaze_core.models.manifest import Manifest
    from genblaze_core.models.run import Run
    from genblaze_core.models.step import Step
    from genblaze_core.sinks.parquet import ParquetSink
    from genblaze_core.storage.base import ObjectLockConfig, StorageBackend

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
        manifest_lock: ObjectLockConfig | None = None,
        pipelined_transfer: bool = False,
        eager_transfer: bool = False,
    ):
        self._backend = backend
        self._prefix = prefix
        self._key_strategy = key_strategy
        is_hierarchical = key_strategy == KeyStrategy.HIERARCHICAL
        asset_prefix = f"{prefix}/runs" if is_hierarchical else f"{prefix}/assets"
        self._transfer = AssetTransfer(
            backend,
            prefix=asset_prefix,
            key_strategy=key_strategy,
            pipelined_transfer=pipelined_transfer,
        )
        self._parquet_sink = parquet_sink
        self._max_upload_workers = max_upload_workers
        self._manifest_object_lock = manifest_lock
        if manifest_lock is not None and manifest_lock.mode == "COMPLIANCE":
            logger.warning(
                "ObjectStorageSink configured with COMPLIANCE-mode Object Lock "
                "until %s. Manifests under this prefix cannot be deleted by "
                "anyone — including the account root — until retention "
                "expires. Bad retention dates cannot be shortened.",
                manifest_lock.retain_until.isoformat(),
            )
        # Lock protects only the manifest write (check-then-put must be atomic)
        self._manifest_lock = threading.Lock()

        # Eager transfer state — see on_step_complete. Pool is lazily
        # created on first use so non-eager sinks pay nothing.
        self._eager_transfer = eager_transfer
        self._eager_pool: ThreadPoolExecutor | None = None
        self._eager_pending: dict[str, Future] = {}
        self._eager_lock = threading.Lock()

    def write_run(self, run: Run, manifest: Manifest) -> None:
        try:
            self._write_run_impl(run, manifest)
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"ObjectStorageSink failed: {exc}") from exc

    def on_step_complete(
        self,
        step: Step,
        *,
        run_id: str,
        tenant_id: str | None,
        date_str: str,
    ) -> None:
        """Eager-transfer hook — starts asset uploads as soon as a step
        finishes, overlapping upload with subsequent step generation.

        No-op unless ``eager_transfer=True`` on construction. Only
        submits assets from SUCCEEDED steps; failed steps have nothing
        to transfer. ``write_run`` later awaits outstanding futures.
        """
        if not self._eager_transfer:
            return
        if step.status != StepStatus.SUCCEEDED:
            return
        if not step.assets:
            return
        with self._eager_lock:
            if self._eager_pool is None:
                self._eager_pool = ThreadPoolExecutor(
                    max_workers=self._max_upload_workers,
                    thread_name_prefix="genblaze-eager",
                )
            pool = self._eager_pool
            for asset in step.assets:
                if asset.asset_id in self._eager_pending:
                    continue  # already submitted — shouldn't happen, be idempotent
                fut = pool.submit(
                    self._transfer.transfer,
                    asset,
                    tenant=tenant_id,
                    date_str=date_str,
                    run_id=run_id,
                )
                self._eager_pending[asset.asset_id] = fut

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

    def _manifest_cache_control(self) -> str:
        """Cache-Control for manifest uploads.

        CAS manifests are keyed by run_id (immutable once a run finishes).
        HIERARCHICAL manifests share a folder with potentially-rewritable
        assets, so we use a shorter TTL.
        """
        if self._key_strategy == KeyStrategy.CONTENT_ADDRESSABLE:
            return "public, max-age=31536000, immutable"
        return "private, max-age=3600"

    def _write_run_impl(self, run: Run, manifest: Manifest) -> None:
        date_str = run.created_at.strftime("%Y-%m-%d")

        # 1. Resolve transfers. Assets may have been submitted eagerly via
        # on_step_complete while subsequent steps were still generating —
        # we await those here. Any assets not pre-submitted go through the
        # same parallel ThreadPoolExecutor path as before.
        failed_asset_ids: list[str] = []
        all_assets = [(step, asset) for step in run.steps for asset in step.assets]

        # Snapshot + drain eager pending state.
        with self._eager_lock:
            eager_pending = dict(self._eager_pending)
            self._eager_pending.clear()

        # Wait for eagerly-submitted transfers.
        for asset_id, fut in eager_pending.items():
            try:
                fut.result()
            except Exception as exc:
                failed_asset_ids.append(asset_id)
                logger.warning("Eager asset transfer failed for %s: %s", asset_id, exc)

        # Transfer any remaining assets that weren't eager-submitted. Same
        # semantics as the legacy non-eager path.
        remaining = [(s, a) for s, a in all_assets if a.asset_id not in eager_pending]
        if remaining:
            workers = min(self._max_upload_workers, len(remaining))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._transfer.transfer,
                        asset,
                        tenant=run.tenant_id,
                        date_str=date_str,
                        run_id=run.run_id,
                    ): asset
                    for _step, asset in remaining
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

        # 4. Upload manifest JSON. We keep the existence check + lock because
        # B2 buckets are always-versioned: re-putting the same manifest
        # silently accrues versions (the per-run noncurrent-expire lifecycle
        # rule ultimately cleans them up, but we'd rather not create churn in
        # the first place). The manifest is treated as immutable once written.
        manifest_key = self._build_manifest_key(run)
        with self._manifest_lock:
            if not self._backend.exists(manifest_key):
                manifest_json = manifest.to_canonical_json()
                manifest_extra: dict = {"CacheControl": self._manifest_cache_control()}
                if self._manifest_object_lock is not None:
                    manifest_extra.update(self._manifest_object_lock.to_extra_args())
                self._backend.put(
                    manifest_key,
                    manifest_json.encode("utf-8"),
                    content_type="application/json",
                    extra_args=manifest_extra,
                )
                # Durable URL — manifest_uri is itself persisted (in pointer-
                # mode embeds and in the parquet sink), so it must not carry
                # a SigV4 signature or expiry.
                manifest.manifest_uri = self._backend.get_durable_url(manifest_key)
                logger.info("Manifest uploaded: %s", manifest_key)

        # 5. Optionally write to ParquetSink
        if self._parquet_sink is not None:
            self._parquet_sink.write_run(run, manifest)

    def close(self, timeout: float | None = None) -> None:
        """Release storage backend resources.

        Shuts down the eager-transfer pool if one was created. Pipelines
        that errored mid-execution and never called ``write_run`` rely on
        this to flush orphan futures.

        Args:
            timeout: If ``None`` (default), waits indefinitely for all
                in-flight uploads. If set, waits at most ``timeout`` seconds
                for the pool to drain, then gives up — queued-but-not-started
                tasks are cancelled via ``cancel_futures=True``. Running
                HTTP uploads cannot be preempted in Python; they complete
                or exit with the process.
        """
        with self._eager_lock:
            pool = self._eager_pool
            self._eager_pool = None
            pending = list(self._eager_pending.values())
            self._eager_pending.clear()
        if pool is not None:
            if timeout is None:
                pool.shutdown(wait=True)
            else:
                # Stop accepting new work and cancel queued tasks; running
                # tasks keep going but we cap our wait.
                pool.shutdown(wait=False, cancel_futures=True)
                if pending:
                    _futures_wait(pending, timeout=timeout)
        self._backend.close()
        if self._parquet_sink is not None:
            self._parquet_sink.close()
