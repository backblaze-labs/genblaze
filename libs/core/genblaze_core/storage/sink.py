"""ObjectStorageSink — upload assets and manifests to object storage."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import wait as _futures_wait
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import ValidationError

from genblaze_core._utils import MAX_MANIFEST_BYTES, normalize_tenant_id
from genblaze_core.exceptions import (
    ManifestError,
    SinkError,
    StorageError,
    UnverifiedAssetError,
)
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.manifest import Manifest, parse_manifest
from genblaze_core.sinks.base import BaseSink
from genblaze_core.storage.base import KeyStrategy
from genblaze_core.storage.key_builder import KeyBuilder
from genblaze_core.storage.transfer import AssetTransfer
from genblaze_core.storage.url_policy import URLPolicy, URLPolicyError

if TYPE_CHECKING:
    from genblaze_core.models.run import Run
    from genblaze_core.models.step import Step
    from genblaze_core.sinks.parquet import ParquetSink
    from genblaze_core.storage.base import ObjectLockConfig, StorageBackend

logger = logging.getLogger("genblaze.storage.sink")

# Max parallel asset uploads within a single write_run call
_DEFAULT_UPLOAD_WORKERS = 4
_LOG_ID_SAMPLE_SIZE = 20

# Module-level guard for the "no public_url_base on the backend" warning.
# Keyed by (bucket, policy) so:
#   - The same bucket constructed twice (multi-tenant fork pattern) warns once.
#   - Two different buckets each warn once.
#   - A bucket reconfigured between policies warns once per policy.
# Test isolation: tests that exercise this path should clear the set via an
# autouse fixture so ordering doesn't leak state.
_warned_durable_url: set[tuple[str, URLPolicy]] = set()
_warned_durable_url_lock = threading.Lock()

# Sentinel for ``_validate_asset_url_policy``'s single-lookup pattern: lets
# us distinguish "backend doesn't declare ``public_url_base`` at all"
# (non-S3-shaped backend → skip the WARN) from "attribute present but
# None/empty" (S3-like backend with no CDN → WARN). Using a private
# sentinel object instead of ``None`` keeps the three cases unambiguous.
_PUBLIC_URL_BASE_MISSING = object()


def _validation_error_summary(exc: ValidationError) -> str:
    try:
        raw_details = exc.errors(include_input=False, include_url=False)
    except TypeError:
        raw_details = exc.errors()

    details = [
        {key: value for key, value in detail.items() if key not in {"input", "url"}}
        for detail in raw_details
    ]
    items: list[str] = []
    for detail in details[:5]:
        loc_value = detail.get("loc", ())
        if isinstance(loc_value, (str, bytes)) or not isinstance(loc_value, Sequence):
            loc_parts: Sequence[object] = (loc_value,)
        else:
            loc_parts = loc_value
        loc = ".".join(str(part) for part in loc_parts) or "<manifest>"
        items.append(f"{loc}: {detail.get('type', 'validation_error')}")
    suffix = "" if len(details) <= 5 else f"; ... {len(details) - 5} more"
    error_count = getattr(exc, "error_count", None)
    count = error_count() if callable(error_count) else len(details)
    return f"{count} validation error(s): {'; '.join(items)}{suffix}"


def _parse_stored_manifest(key: str, data: bytes) -> Manifest:
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Stored manifest at {key} is not valid JSON: {exc}") from exc

    try:
        return parse_manifest(raw)
    except ManifestError as exc:
        raise ManifestError(f"Stored manifest at {key} is invalid: {exc}") from exc
    except ValidationError as exc:
        raise ManifestError(
            f"Stored manifest at {key} is invalid: {_validation_error_summary(exc)}"
        ) from exc
    except (AttributeError, TypeError) as exc:
        raise ManifestError(f"Stored manifest at {key} is invalid: {type(exc).__name__}") from exc


def _id_log_extra(ids: Sequence[str]) -> dict[str, object]:
    return {
        "count": len(ids),
        "sample": list(ids[:_LOG_ID_SAMPLE_SIZE]),
        "sample_truncated": len(ids) > _LOG_ID_SAMPLE_SIZE,
    }


def _verify_stored_manifest(
    key: str,
    manifest: Manifest,
    *,
    verify: bool,
    allow_unverified_assets: bool,
) -> None:
    if not verify:
        return
    if not manifest.verify_hash():
        raise ManifestError(f"Stored manifest at {key} fails canonical_hash verification")

    if manifest.transfer_failures:
        logger.warning(
            "Stored manifest has transfer failures",
            extra={
                "manifest_key": key,
                "run_id": manifest.run.run_id,
                "transfer_failures": _id_log_extra(manifest.transfer_failures),
            },
        )

    missing_sha_ids = manifest.output_asset_ids_missing_sha256()
    if missing_sha_ids:
        logger.warning(
            "Stored manifest has output assets missing or malformed sha256",
            extra={
                "manifest_key": key,
                "run_id": manifest.run.run_id,
                "asset_ids": _id_log_extra(missing_sha_ids),
            },
        )
        if not allow_unverified_assets:
            raise UnverifiedAssetError(
                f"Stored manifest at {key} has {len(missing_sha_ids)} "
                "output asset(s) missing or malformed sha256",
                asset_ids=missing_sha_ids,
            )


def _require_asset_id(asset_id: str) -> str:
    try:
        parsed = uuid.UUID(asset_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SinkError(f"asset_id must be a UUID, got {asset_id!r}") from exc
    return str(parsed)


def _normalize_tenant_id_or_sink_error(tenant_id: object, *, message: str) -> str | None:
    if tenant_id is not None and not isinstance(tenant_id, str):
        raise SinkError(message)
    return normalize_tenant_id(tenant_id)


def _require_tenant_id(tenant_id: object) -> str:
    tenant = _normalize_tenant_id_or_sink_error(tenant_id, message="tenant_id must be a string")
    if tenant is None:
        raise SinkError("tenant_id is required for asset manifest reverse lookup")
    return tenant


def _tenant_index_segment(tenant_id: str) -> str:
    return quote(tenant_id, safe="")


def _warn_durable_url_on_private_bucket(bucket: str, policy: URLPolicy) -> None:
    """Emit a one-time WARN about durable-URL behavior on a private bucket.

    Fired from ``ObjectStorageSink.__init__`` when the backend's
    ``public_url_base`` is unset under ``URLPolicy.AUTO``. Module-level
    dedup so a process running many sinks against the same bucket only
    sees one warning.
    """
    key = (bucket, policy)
    # Double-checked locking: the unlocked read is fast and correct for
    # the steady-state "already warned" case; the lock guards check-then-add
    # against a TOCTOU race that could otherwise double-emit under
    # high-concurrency sink construction.
    if key in _warned_durable_url:
        return
    with _warned_durable_url_lock:
        if key in _warned_durable_url:
            return
        _warned_durable_url.add(key)
    logger.warning(
        "ObjectStorageSink: backend has no public_url_base configured for "
        "bucket %r. asset.url will be the durable endpoint URL — browsers "
        "may 403 on private buckets. Configure backend.public_url_base, or "
        "read assets via backend.presigned_get_url(key) at fetch time.",
        bucket,
    )


class ObjectStorageSink(BaseSink):
    """Upload run assets and manifests to an object storage backend.

    Optionally delegates to a ParquetSink for structured data, and can
    upload the resulting Parquet files to storage as well.
    """

    @staticmethod
    def _validate_asset_url_policy(backend: StorageBackend, policy: URLPolicy) -> None:
        """Validate ``asset_url_policy`` against the backend's configuration.

        Three cases:

        * ``PRESIGNED`` — rejected outright. Writing SigV4 URLs into
          ``asset.url`` would embed expiring credentials in manifests,
          breaking provenance (the URL decays before the manifest does).
          Caller is pointed at ``backend.presigned_get_url(key)`` for
          per-asset read-time presigning.
        * ``PUBLIC`` — requires ``backend.public_url_base`` to be set.
          Otherwise raises so misconfiguration fails loudly at construction
          rather than silently producing 403-on-fetch URLs.
        * ``AUTO`` — preserves today's durable-URL behavior. If the
          backend has no ``public_url_base``, emits a one-time WARN to
          alert the caller (private buckets need ``public_url_base`` or
          read-time presigning). Backends without a ``public_url_base``
          attribute at all (non-S3-shaped backends) skip the WARN.
        """
        if policy is URLPolicy.PRESIGNED:
            raise URLPolicyError(
                "asset_url_policy=URLPolicy.PRESIGNED is not supported on "
                "ObjectStorageSink. For read-time presigned URLs, call "
                "backend.presigned_get_url(key) directly when handing the "
                "URL to an HTTP client. (Reason: manifests outlive presigned "
                "SigV4 URLs, so persisting them breaks provenance.)"
            )
        # Single attribute lookup with a sentinel so the "attribute missing"
        # branch (non-S3-shaped backends) is distinguishable from the
        # "attribute present but empty or None" branch (S3-like backends
        # without a CDN configured). Treat the empty string the same as
        # None — both indicate "not configured" — so PUBLIC raises and AUTO
        # warns consistently for either misconfiguration.
        public_url_base = getattr(backend, "public_url_base", _PUBLIC_URL_BASE_MISSING)
        if policy is URLPolicy.PUBLIC:
            if public_url_base is _PUBLIC_URL_BASE_MISSING:
                # Backend doesn't expose ``public_url_base`` at all — most
                # non-S3-shaped backends. PUBLIC mode is meaningless here;
                # fail loudly rather than silently constructing a sink that
                # would produce durable-only URLs under a policy that
                # promised public ones.
                raise URLPolicyError(
                    "asset_url_policy=URLPolicy.PUBLIC requires a backend "
                    "that exposes a public_url_base attribute. This backend "
                    f"({type(backend).__name__}) does not. Use "
                    "asset_url_policy=URLPolicy.AUTO instead, or pass an "
                    "S3-compatible backend with public_url_base configured."
                )
            if not public_url_base:
                raise URLPolicyError(
                    "asset_url_policy=URLPolicy.PUBLIC requires "
                    "backend.public_url_base to be set (got "
                    f"{public_url_base!r}). Pass "
                    "asset_url_policy=URLPolicy.AUTO to fall back to the "
                    "durable endpoint URL, or configure public_url_base on "
                    "the backend (e.g. via S3StorageBackend.for_backblaze("
                    "public_url_base=...))."
                )
        if (
            policy is URLPolicy.AUTO
            and public_url_base is not _PUBLIC_URL_BASE_MISSING
            and not public_url_base
        ):
            # The backend declares a ``public_url_base`` attribute but it's
            # unset (None) or empty (""). Both indicate "user picked an
            # S3-like backend, didn't wire a CDN/public-URL base, and is
            # about to ship durable-only URLs that may 403 in browsers."
            bucket = (
                getattr(backend, "_bucket", None)
                or getattr(backend, "bucket", None)
                or "<unknown>"
            )
            _warn_durable_url_on_private_bucket(bucket, policy)

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
        asset_url_policy: URLPolicy = URLPolicy.AUTO,
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
            asset_url_policy: Selects what flavor of URL gets written into
                ``asset.url`` on transfer. Default :class:`URLPolicy.AUTO`
                preserves today's behavior (durable, credential-free URL
                from ``backend.get_durable_url``). :class:`URLPolicy.PUBLIC`
                enforces that ``backend.public_url_base`` is configured
                (raises :class:`URLPolicyError` at construction if not).
                :class:`URLPolicy.PRESIGNED` is **rejected at construction**
                — manifests must not carry SigV4 URLs (they decay before
                the manifest does, breaking provenance). For read-time
                presigned URLs use ``backend.presigned_get_url(key)``
                directly. Introduced in ``genblaze-core`` 0.3.1.

        Raises:
            URLPolicyError: ``asset_url_policy=URLPolicy.PRESIGNED`` (rejected;
                manifests cannot carry credential-bearing URLs). Or
                ``asset_url_policy=URLPolicy.PUBLIC`` when the backend has
                no ``public_url_base`` set.
        """
        self._validate_asset_url_policy(backend, asset_url_policy)
        self._asset_url_policy = asset_url_policy
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

    def read_manifest(
        self,
        run: Run,
        *,
        verify: bool = True,
        allow_unverified_assets: bool = False,
    ) -> Manifest:
        """Fetch and parse the stored manifest for this run.

        Args:
            run: The run whose manifest to load. Only ``run_id`` /
                ``tenant_id`` / ``created_at`` are used (to derive the key).
            verify: When True (default), checks hash integrity and output
                asset ``sha256`` declarations. Pass ``verify=False`` to skip
                only those hash and output-sha256 checks on a manifest you
                trust (e.g. one you just wrote). Stored bytes are always JSON
                decoded and parsed through ``parse_manifest()``, so schema
                validation and manifest invariants still apply.
            allow_unverified_assets: When True, ``verify=True`` still checks
                ``manifest.verify_hash()`` but allows output assets whose
                ``sha256`` is missing or malformed. This is the explicit
                hash-only read path for callers that need to inspect partially
                transferred or historical manifests. Treat this as
                security-sensitive; do not bind it directly to request or
                tenant-controlled input.

        Raises:
            SinkError: when the stored object exceeds ``MAX_MANIFEST_BYTES``.
                Bounds OOM blast from a malicious or corrupt object.
            ManifestError: when ``verify=True`` and hash integrity fails.
            UnverifiedAssetError: when output assets have missing or malformed
                ``sha256`` without ``allow_unverified_assets=True``.
        """
        key = self.manifest_key_for(run)
        data = self._backend.get(key)
        if len(data) > MAX_MANIFEST_BYTES:
            raise SinkError(
                f"Stored manifest at {key} is {len(data)} bytes, exceeds "
                f"MAX_MANIFEST_BYTES={MAX_MANIFEST_BYTES}"
            )
        manifest = _parse_stored_manifest(key, data)
        _verify_stored_manifest(
            key,
            manifest,
            verify=verify,
            allow_unverified_assets=allow_unverified_assets,
        )
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
        manifest.assert_writable_schema()
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
        # non-hashed Manifest field, so writing it here doesn't affect the
        # canonical payload. Any failed asset that remains URL-only will still
        # make Manifest.verify() return False for output sha256 coverage.
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
        tenant_id: str | None = None,
    ) -> Asset:
        """Write a single asset to the backend (no Run wrapper required).

        Reuses the existing :class:`AssetTransfer` machinery to download
        the source bytes (``file://`` or ``https://``), hash them, build
        the storage key per the sink's ``key_strategy``, and upload.
        After success the asset is mutated: ``url`` becomes the
        backend's durable URL and ``sha256`` / ``size_bytes`` /
        ``media_type`` are populated when missing.

        When both ``manifest_uri`` and ``tenant_id`` are supplied, a
        tenant-scoped sidecar index entry is written so
        :meth:`read_manifest_for_asset` can discover the manifest later.
        Pass both for assets that ARE referenced by a manifest; omit
        ``manifest_uri`` for one-off uploads (DAM ingest with no
        manifest). Supplying ``manifest_uri`` without ``tenant_id`` is
        rejected because a global asset-id index is an authorization
        boundary in multi-tenant deployments.
        """
        # Drive the existing transfer pipeline. When a tenant is supplied,
        # pass it through so HIERARCHICAL standalone asset keys remain scoped
        # consistently with write_run(); CAS ignores tenant/date/run_id.
        tenant = _normalize_tenant_id_or_sink_error(
            tenant_id, message="tenant_id must be a string"
        )
        if manifest_uri is not None and tenant is None:
            raise SinkError("tenant_id is required for asset manifest reverse lookup")
        self._transfer.transfer(asset, tenant=tenant)
        if manifest_uri is not None:
            self._write_asset_index(asset.asset_id, manifest_uri, tenant_id=tenant)
        return asset

    def put_assets(
        self,
        assets: Sequence[Asset],
        *,
        manifest_uri: str | None = None,
        tenant_id: str | None = None,
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
                pool.submit(
                    self.put_asset,
                    asset,
                    manifest_uri=manifest_uri,
                    tenant_id=tenant_id,
                )
                for asset in assets_list
            ]
            return [fut.result() for fut in futures]

    def read_manifest_for_asset(
        self,
        asset_id: str,
        *,
        tenant_id: str,
        verify: bool = True,
        allow_unverified_assets: bool = False,
    ) -> Manifest | None:
        """Reverse lookup: ``asset_id`` → :class:`Manifest`.

        Reads the tenant-scoped sidecar index at
        ``{prefix}/_index/{tenant_id}/{asset_id}.json``
        written by :meth:`put_asset` (when ``manifest_uri=`` was
        supplied), then fetches and parses the manifest from the
        recorded URI. Returns ``None`` when no index entry exists or the
        referenced manifest belongs to a different tenant.

        Manifests for assets put without ``manifest_uri=`` are not
        discoverable via this method — by design. Callers needing
        guaranteed discoverability MUST pass both ``manifest_uri=`` and
        ``tenant_id=`` to :meth:`put_asset`.
        """
        tenant = _require_tenant_id(tenant_id)
        index_key = self._asset_index_key(asset_id, tenant_id=tenant)
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
        if not isinstance(entry, dict):
            raise SinkError(f"Asset index at {index_key!r} must be a JSON object")
        index_tenant = _normalize_tenant_id_or_sink_error(
            entry.get("tenant_id"),
            message=f"Asset index at {index_key!r} has non-string tenant_id",
        )
        if index_tenant != tenant:
            logger.warning(
                "Asset manifest reverse lookup denied for index tenant mismatch",
                extra={
                    "index_key": index_key,
                    "requested_tenant_id": tenant,
                    "index_tenant_id": index_tenant,
                },
            )
            return None
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
        manifest = _parse_stored_manifest(manifest_key, data)
        if normalize_tenant_id(manifest.run.tenant_id) != tenant:
            logger.warning(
                "Asset manifest reverse lookup denied for tenant mismatch",
                extra={
                    "index_key": index_key,
                    "manifest_key": manifest_key,
                    "requested_tenant_id": tenant,
                    "manifest_tenant_id": manifest.run.tenant_id,
                },
            )
            return None
        _verify_stored_manifest(
            manifest_key,
            manifest,
            verify=verify,
            allow_unverified_assets=allow_unverified_assets,
        )
        return manifest

    def _asset_index_key(self, asset_id: str, *, tenant_id: str) -> str:
        """Storage key for the asset_id → manifest_uri sidecar."""
        asset_id = _require_asset_id(asset_id)
        tenant = _require_tenant_id(tenant_id)
        return self._kb.build("_index", _tenant_index_segment(tenant), f"{asset_id}.json")

    def _write_asset_index(
        self,
        asset_id: str,
        manifest_uri: str,
        *,
        tenant_id: str | None,
    ) -> None:
        """Write the sidecar index entry. Idempotent — re-writes
        the same key on repeat calls (for the same asset, the
        manifest_uri is expected to be stable)."""
        tenant = _require_tenant_id(tenant_id)
        asset_id = _require_asset_id(asset_id)
        payload = json.dumps({"manifest_uri": manifest_uri, "tenant_id": tenant}).encode("utf-8")
        self._backend.put(
            self._asset_index_key(asset_id, tenant_id=tenant),
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
