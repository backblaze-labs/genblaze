"""ObjectStorageSink — upload assets and manifests to object storage."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import wait as _futures_wait
from typing import TYPE_CHECKING

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import ManifestError, SinkError, StorageError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.sinks.base import BaseSink
from genblaze_core.storage.base import KeyStrategy
from genblaze_core.storage.key_builder import KeyBuilder
from genblaze_core.storage.transfer import AssetTransfer

if TYPE_CHECKING:
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
        """Construct an ObjectStorageSink.

        Args:
            backend: S3-compatible storage backend.
            prefix: Root prefix for all keys. Default ``"genblaze"``.
                **Phase 1C (0.3.0):** ``prefix="runs"`` no longer doubles the
                ``runs/`` segment under :class:`KeyStrategy.HIERARCHICAL`.
                Previously the strategy's hardcoded ``runs/`` was concatenated
                onto a prefix that already ended in ``runs``, producing
                ``runs/runs/{tenant}/{date}/{run_id}/...`` — a typo-shaped
                layout most callers actually wanted to avoid. Path
                normalization happens at the prefix↔strategy seam only;
                callers who intentionally double a segment within the
                prefix (e.g. ``"archive/archive"``) keep that behavior.
            key_strategy: HIERARCHICAL groups everything per-run; the default
                CONTENT_ADDRESSABLE deduplicates assets by SHA-256.
            parquet_sink: Optional structured-data sibling sink.
            max_upload_workers: Max parallel asset uploads per ``write_run``.
            manifest_lock: Optional Object Lock retention applied to manifests.
            pipelined_transfer: Pipelined CAS transfer (temp → copy → rename).
            eager_transfer: Start asset uploads from ``on_step_complete``
                instead of waiting for ``write_run``.
        """
        self._backend = backend
        self._prefix = prefix
        self._key_strategy = key_strategy
        # Single source of key normalization for both manifest and asset
        # paths — the seam-dedupe rule lives in KeyBuilder, not in ad-hoc
        # f-strings spread across sink + transfer.
        self._kb = KeyBuilder.from_prefix(prefix)
        is_hierarchical = key_strategy == KeyStrategy.HIERARCHICAL
        asset_kb = self._kb.append("runs" if is_hierarchical else "assets")
        self._transfer = AssetTransfer(
            backend,
            prefix=asset_kb.prefix,
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

    def manifest_key_for(self, run: Run) -> str:
        """Storage key where this run's manifest is (or would be) written.

        Pure function of ``run`` + sink config — does not touch the backend.
        Public so app code can locate or precompute the manifest URL/key
        without re-implementing the layout rules.
        """
        if self._key_strategy == KeyStrategy.HIERARCHICAL:
            parts = ["runs"]
            if run.tenant_id:
                parts.append(run.tenant_id)
            parts.append(run.created_at.strftime("%Y-%m-%d"))
            parts.append(run.run_id)
            parts.append("manifest.json")
            return self._kb.build(*parts)
        return self._kb.build("manifests", f"{run.run_id}.json")

    # Internal alias kept so existing call sites don't churn in this PR.
    _build_manifest_key = manifest_key_for

    def manifest_url_for(self, run: Run) -> str:
        """Durable, credential-free URL for this run's manifest.

        Equivalent to ``self._backend.get_durable_url(self.manifest_key_for(run))``.
        Useful when the caller wants a publishable link without first having
        called ``write_run`` (or when the in-memory ``manifest.manifest_uri``
        is unavailable).
        """
        return self._backend.get_durable_url(self.manifest_key_for(run))

    def read_manifest(self, run: Run, *, verify: bool = True) -> Manifest:
        """Fetch and parse the stored manifest for this run.

        Args:
            run: The run whose manifest to load. Only ``run_id`` /
                ``tenant_id`` / ``created_at`` are used (to derive the key).
            verify: When True (default), checks ``manifest.verify()`` and
                raises :class:`ManifestError` on hash mismatch. Pass
                ``verify=False`` to skip the rehash on a manifest you
                trust (e.g. one you just wrote).

        Raises:
            SinkError: when the stored object exceeds ``MAX_MANIFEST_BYTES``.
                Bounds OOM blast from a malicious or corrupt object.
            ManifestError: when ``verify=True`` and the hash doesn't match.
        """
        key = self.manifest_key_for(run)
        data = self._backend.get(key)
        if len(data) > MAX_MANIFEST_BYTES:
            raise SinkError(
                f"Stored manifest at {key} is {len(data)} bytes, exceeds "
                f"MAX_MANIFEST_BYTES={MAX_MANIFEST_BYTES}"
            )
        manifest = Manifest.model_validate_json(data)
        if verify and not manifest.verify():
            raise ManifestError(f"Stored manifest at {key} fails canonical_hash verification")
        return manifest

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
        manifest_key = self.manifest_key_for(run)
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
                logger.info("Manifest uploaded: %s", manifest_key)

        # Durable URL — manifest_uri is itself persisted (in pointer-mode
        # embeds and in the parquet sink), so it must not carry a SigV4
        # signature or expiry. Set unconditionally outside the lock: pointer-
        # mode embedders fail if it's None, and the previous version skipped
        # this assignment when the object already existed (e.g. on retries
        # against the same run). get_durable_url is idempotent and takes its
        # own region-verification lock, so calling it here is safe.
        manifest.manifest_uri = self._backend.get_durable_url(manifest_key)

        # 5. Optionally write to ParquetSink
        if self._parquet_sink is not None:
            self._parquet_sink.write_run(run, manifest)

    # ------------------------------------------------------------------
    # Plan 4 Phase 1 — standalone asset writes
    # ------------------------------------------------------------------

    def put_asset(
        self,
        asset: Asset,
        *,
        manifest_uri: str | None = None,
    ) -> Asset:
        """Write a single asset to the backend (no Run wrapper required).

        Reuses the existing :class:`AssetTransfer` machinery to download
        the source bytes (``file://`` or ``https://``), hash them, build
        the storage key per the sink's ``key_strategy``, and upload.
        After success the asset is mutated: ``url`` becomes the
        backend's durable URL and ``sha256`` / ``size_bytes`` /
        ``media_type`` are populated when missing.

        When ``manifest_uri`` is supplied, a sidecar index entry is
        written at ``{prefix}/_index/{asset_id}.json`` so
        :meth:`read_manifest_for_asset` can discover the manifest
        later. Pass ``manifest_uri`` for assets that ARE referenced by
        a manifest; omit for one-off uploads (DAM ingest with no
        manifest).
        """
        # Drive the existing transfer pipeline. No tenant/date/run_id —
        # under HIERARCHICAL the strategy degrades to {prefix}/runs/assets/...
        # which is fine for standalone writes; under CAS the layout is
        # hash-keyed and tenant/date/run_id were always ignored anyway.
        self._transfer.transfer(asset)
        if manifest_uri is not None:
            self._write_asset_index(asset.asset_id, manifest_uri)
        return asset

    def put_assets(
        self,
        assets: Sequence[Asset],
        *,
        manifest_uri: str | None = None,
    ) -> list[Asset]:
        """Bulk variant of :meth:`put_asset`.

        Parallelizes via a fresh ``ThreadPoolExecutor`` sized at
        ``min(max_upload_workers, len(assets))``. Returned list
        preserves input order regardless of completion order; if any
        per-asset transfer fails, the exception propagates after the
        pool drains (as opposed to ``write_run`` which records
        ``transfer_failures`` on the manifest — there's no manifest
        here).
        """
        assets_list = list(assets)
        if not assets_list:
            return []
        workers = min(self._max_upload_workers, len(assets_list))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Preserve input order: index → future.
            futures = [
                pool.submit(self.put_asset, asset, manifest_uri=manifest_uri)
                for asset in assets_list
            ]
            return [fut.result() for fut in futures]

    def read_manifest_for_asset(self, asset_id: str) -> Manifest | None:
        """Reverse lookup: ``asset_id`` → :class:`Manifest`.

        Reads the sidecar index at ``{prefix}/_index/{asset_id}.json``
        written by :meth:`put_asset` (when ``manifest_uri=`` was
        supplied), then fetches and parses the manifest from the
        recorded URI. Returns ``None`` when no index entry exists.

        Manifests for assets put without ``manifest_uri=`` are not
        discoverable via this method — by design. Callers needing
        guaranteed discoverability MUST pass ``manifest_uri=`` to
        :meth:`put_asset`.
        """
        index_key = self._asset_index_key(asset_id)
        if not self._backend.exists(index_key):
            return None
        try:
            raw = self._backend.get(index_key)
        except StorageError:
            return None
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SinkError(f"Asset index at {index_key!r} is not valid JSON: {exc}") from exc
        manifest_uri = entry.get("manifest_uri")
        if not manifest_uri:
            return None
        # The index points at a manifest_uri — the manifest itself was
        # written at the corresponding key. Round-trip via the backend.
        manifest_key = self._backend.key_from_url(manifest_uri)
        if manifest_key is None:
            # Foreign URL — not on this backend. Caller can fetch
            # themselves; this method only discovers same-backend
            # manifests.
            return None
        data = self._backend.get(manifest_key)
        if len(data) > MAX_MANIFEST_BYTES:
            raise SinkError(
                f"Stored manifest at {manifest_key} is {len(data)} bytes, "
                f"exceeds MAX_MANIFEST_BYTES={MAX_MANIFEST_BYTES}"
            )
        return Manifest.model_validate_json(data)

    def _asset_index_key(self, asset_id: str) -> str:
        """Storage key for the asset_id → manifest_uri sidecar."""
        return self._kb.build("_index", f"{asset_id}.json")

    def _write_asset_index(self, asset_id: str, manifest_uri: str) -> None:
        """Write the sidecar index entry. Idempotent — re-writes
        the same key on repeat calls (for the same asset, the
        manifest_uri is expected to be stable)."""
        payload = json.dumps({"manifest_uri": manifest_uri}).encode("utf-8")
        self._backend.put(
            self._asset_index_key(asset_id),
            payload,
            content_type="application/json",
        )

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
