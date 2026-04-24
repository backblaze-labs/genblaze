"""S3StorageBackend — works with any S3-compatible service (B2, R2, MinIO, AWS)."""

from __future__ import annotations

import io
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, BinaryIO

from botocore.exceptions import ClientError
from genblaze_core._version import __version__
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.base import StorageBackend

if TYPE_CHECKING:
    pass

logger = logging.getLogger("genblaze.s3")

# User agent for B2/S3 API tracking
_USER_AGENT = f"b2ai-genblaze/{__version__}"

# Single-PUT vs multipart cutoff. Above this, boto3 splits into 16 MB parts
# and uploads up to _MAX_CONCURRENCY in parallel — each part individually
# retryable, which matters for multi-GB video payloads on flaky links.
_MULTIPART_THRESHOLD = 16 * 1024 * 1024
_MULTIPART_CHUNKSIZE = 16 * 1024 * 1024
_MAX_CONCURRENCY = 4

# urllib3 connection pool ceiling. With 4 asset workers × 4 part workers we
# can saturate 16 concurrent connections; headroom for HEAD/auth prefetch.
_MAX_POOL_CONNECTIONS = 20

# Lifecycle defaults applied by ensure_lifecycle_defaults() / auto_lifecycle=True.
# Orphaned multipart uploads from mid-stream failures otherwise sit billable
# indefinitely — a real cost vector once the multipart path is the default.
_DEFAULT_CANCEL_MULTIPART_DAYS = 7
_DEFAULT_NONCURRENT_EXPIRE_DAYS = 30


def _build_boto_config() -> Any:
    """BotoConfig factory — shared by the client constructor and reconfigure path.

    ``request/response_checksum_calculation="when_required"`` disables the
    default CRC32 trailer that boto3 >= 1.36 injects. That header broke B2
    uploads until B2 added support in July 2025; keeping it off avoids the
    landmine across any S3-compat endpoint that lags on checksum-header
    support (older B2, MinIO, Wasabi). We pass SHA-256 explicitly per upload
    via ExtraArgs instead.

    Explicit connect/read timeouts exist because boto3's 60s default
    read_timeout can fire mid-upload on slow links with GB-sized video payloads.
    """
    from botocore.config import Config as BotoConfig

    return BotoConfig(
        user_agent_extra=_USER_AGENT,
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=30,
        read_timeout=300,
        max_pool_connections=_MAX_POOL_CONNECTIONS,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


class S3StorageBackend(StorageBackend):
    """S3-compatible object storage backend.

    Supports Backblaze B2, Cloudflare R2, MinIO, and AWS S3 via endpoint_url.

    Args:
        bucket: S3 bucket name.
        endpoint_url: S3-compatible endpoint (e.g. "https://s3.us-west-004.backblazeb2.com").
            None for standard AWS S3.
        region: AWS region name (e.g. "us-west-004").
        public_url_base: Base URL for public/friendly URLs (e.g. B2 friendly URL).
            If set, get_url() returns {public_url_base}/{key} instead of pre-signed URLs.
        aws_access_key_id: Override credentials (else uses boto3 defaults).
        aws_secret_access_key: Override credentials.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        public_url_base: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        self._bucket = bucket
        self._public_url_base = public_url_base.rstrip("/") if public_url_base else None
        # Region may be updated later by auto-detection (see _ensure_region_verified).
        self._region = region
        self._region_verified = False
        # Serializes first-use preflight. ObjectStorageSink calls put() from a
        # ThreadPoolExecutor, so multiple workers race through the preflight
        # on a fresh backend. Without the lock we'd issue N HEADs and N
        # client rebuilds instead of 1.
        self._region_lock = threading.Lock()
        # Sticky non-redirect preflight failure (bad creds, wrong bucket, etc.)
        # — cache once so call 2+ gets the same helpful message as call 1
        # without re-HEADing.
        self._preflight_error: StorageError | None = None
        # boto3's Config object does NOT hold credentials (they live in the
        # session's resolver), so we can't recover them from self._client
        # after construction — persist them explicitly for _reconfigure_for_region.
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._endpoint_url = endpoint_url

        try:
            import boto3
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for S3StorageBackend. "
                'Install it with: pip install "genblaze-s3"'
            ) from exc

        self._client = boto3.client("s3", **self._client_kwargs())
        self._transfer_config = self._build_transfer_config()

    @property
    def _is_b2(self) -> bool:
        """True when the endpoint points at B2 — gates B2-specific behaviors."""
        return bool(self._endpoint_url and "backblazeb2.com" in self._endpoint_url)

    def _client_kwargs(self) -> dict[str, Any]:
        """Assemble boto3.client kwargs from current instance state."""
        kwargs: dict[str, Any] = {"config": _build_boto_config()}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._region:
            kwargs["region_name"] = self._region
        if self._aws_access_key_id:
            kwargs["aws_access_key_id"] = self._aws_access_key_id
        if self._aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self._aws_secret_access_key
        return kwargs

    @staticmethod
    def _build_transfer_config() -> Any:
        """TransferConfig factory — shared by __init__ and _reconfigure_for_region."""
        from boto3.s3.transfer import TransferConfig

        return TransferConfig(
            multipart_threshold=_MULTIPART_THRESHOLD,
            multipart_chunksize=_MULTIPART_CHUNKSIZE,
            max_concurrency=_MAX_CONCURRENCY,
            use_threads=True,
        )

    def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        extra_args: dict[str, Any] | None = None,
    ) -> str:
        """Upload an object to S3. Returns the storage URL.

        Uses boto3's managed ``upload_fileobj`` so large payloads are
        automatically split into multipart uploads (16 MB parts, 4-way
        parallel) and each part is individually retryable. Per-part SHA-256
        checksums are negotiated server-side via ``ChecksumAlgorithm``.

        If the caller pins an explicit ``ChecksumSHA256`` via ``extra_args``,
        we route through ``put_object`` (single-PUT) instead — whole-object
        SHA-256 checksums are only valid on single-part uploads. Callers
        that need whole-object verification on large payloads should
        compute and pass it with a ``bytes`` payload under the multipart
        threshold, or omit and rely on the per-part default.
        """
        try:
            self._ensure_region_verified()
            merged = self._build_extra_args(content_type, metadata, extra_args)
            if "ChecksumSHA256" in merged:
                return self._put_single(key, data, merged)
            stream: BinaryIO = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
            self._client.upload_fileobj(
                stream,
                self._bucket,
                key,
                ExtraArgs=merged,
                Config=self._transfer_config,
            )
            return self.get_url(key)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 put failed for {key}: {exc}") from exc

    def _put_single(
        self,
        key: str,
        data: bytes | BinaryIO,
        extra_args: dict[str, Any],
    ) -> str:
        """Single-PUT path used when the caller pinned a whole-object checksum."""
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra_args)
        return self.get_url(key)

    @staticmethod
    def _build_extra_args(
        content_type: str | None,
        metadata: dict[str, str] | None,
        extra_args: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Merge kwargs into a boto3-style ExtraArgs dict, honoring caller overrides."""
        merged: dict[str, Any] = {}
        if content_type:
            merged["ContentType"] = content_type
        if metadata:
            merged["Metadata"] = metadata
        # Caller-provided keys win (including overriding ChecksumAlgorithm).
        if extra_args:
            merged.update(extra_args)
        # Default to SHA-256 per-part integrity unless caller pinned a checksum.
        if "ChecksumAlgorithm" not in merged and "ChecksumSHA256" not in merged:
            merged["ChecksumAlgorithm"] = "SHA256"
        return merged

    def get(self, key: str) -> bytes:
        """Download an object from S3."""
        try:
            self._ensure_region_verified()
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 get failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        """Check if an object exists in S3.

        Treats 404 and 403/AccessDenied as "does not exist". Scoped B2/AWS
        application keys commonly have ReadFiles without ListFiles, which
        returns 403 for HEAD on non-existent keys. Raising would break CAS
        dedup for least-privilege credentials — we log at DEBUG so real
        permission failures still leave a breadcrumb.
        """
        try:
            self._ensure_region_verified()
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "404":
                return False
            if code in ("403", "AccessDenied"):
                logger.debug(
                    "head_object %s returned 403/AccessDenied — treating as not-exist. "
                    "If the key should be visible, check bucket/prefix permissions.",
                    key,
                )
                return False
            raise StorageError(f"S3 exists check failed for {key}: {exc}") from exc
        except StorageError:
            raise

    def delete(self, key: str) -> None:
        """Delete an object from S3."""
        try:
            self._ensure_region_verified()
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 delete failed for {key}: {exc}") from exc

    def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy — bytes never transit the client.

        Used by the pipelined CAS transfer path to promote a temp upload
        to its content-addressed final key once the hash is known. B2's
        S3 API supports this natively and charges nothing for server-side
        bandwidth (just two transaction class-C calls).

        Note: single-call ``copy_object`` has a **5 GB source limit**
        per AWS S3 semantics (B2 matches). Objects larger than that
        require ``UploadPartCopy`` multipart orchestration — future
        work. Until then, assets approaching the 5 GB
        ``_DEFAULT_MAX_DOWNLOAD_BYTES`` ceiling should prefer
        HIERARCHICAL key strategy with pipelined_transfer, which
        skips the copy step entirely.
        """
        try:
            self._ensure_region_verified()
            self._client.copy_object(
                Bucket=self._bucket,
                Key=dst_key,
                CopySource={"Bucket": self._bucket, "Key": src_key},
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 copy failed for {src_key} -> {dst_key}: {exc}") from exc

    def get_url(self, key: str, *, expires_in: int = 3600) -> str:
        """Get a short-lived URL — pre-signed unless ``public_url_base`` is set.

        Do NOT persist the result. Presigned URLs leak the access key ID
        (``X-Amz-Credential``) and grant a time-limited fetch capability;
        they also break canonical-hash stability if hashed. For anything
        landing in a manifest, parquet sink, or embedded media payload,
        call :meth:`get_durable_url` instead.
        """
        if self._public_url_base:
            from urllib.parse import quote

            return f"{self._public_url_base}/{quote(key, safe='/')}"
        try:
            self._ensure_region_verified()
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 get_url failed for {key}: {exc}") from exc

    def get_durable_url(self, key: str) -> str:
        """Return a credential-free, never-expiring URL safe to persist.

        Uses ``public_url_base`` when set (e.g. B2 friendly URL).
        Otherwise builds the canonical S3 path from the verified endpoint.
        The URL alone does not grant access — consumers either rely on
        public bucket read or call :meth:`get_url` later for a presigned
        fetch. This is what gets written into ``asset.url`` so manifests
        and embedded media never carry SigV4 signatures.
        """
        from urllib.parse import quote

        encoded = quote(key, safe="/")
        if self._public_url_base:
            return f"{self._public_url_base}/{encoded}"
        # Verify region first so endpoint_url is the correct one (B2 buckets
        # may live in a different region than the constructor hint).
        self._ensure_region_verified()
        endpoint = (self._client.meta.endpoint_url or "").rstrip("/")
        return f"{endpoint}/{self._bucket}/{encoded}"

    def _ensure_region_verified(self) -> None:
        """Lazy-verify the bucket region on first use; follow redirect if wrong.

        B2 (and AWS) return 301 ``PermanentRedirect`` with an
        ``x-amz-bucket-region`` header when the client is pointed at the
        wrong region. For B2 endpoints we rewrite to the correct B2 regional
        endpoint — users stop needing to hand-pick the right ``region=`` on
        ``for_backblaze``. For other endpoints (AWS S3, R2, MinIO) we don't
        attempt to rebuild the endpoint URL since we can't synthesize it;
        we just surface the error with guidance.

        Uses double-checked locking so concurrent first-use callers (the
        ``ObjectStorageSink`` thread pool) only run preflight once.
        """
        # Fast path — uncontended attribute read, no lock.
        if self._region_verified:
            if self._preflight_error is not None:
                raise self._preflight_error
            return

        with self._region_lock:
            # Second check after acquiring the lock — another thread may
            # have verified while we were waiting.
            if self._region_verified:
                if self._preflight_error is not None:
                    raise self._preflight_error
                return

            try:
                self._client.head_bucket(Bucket=self._bucket)
                self._region_verified = True
                return
            except ClientError as exc:
                actual = (
                    exc.response.get("ResponseMetadata", {})
                    .get("HTTPHeaders", {})
                    .get("x-amz-bucket-region")
                )
                code = exc.response.get("Error", {}).get("Code")
                is_redirect = code in {"301", "PermanentRedirect"}
                if self._is_b2 and is_redirect and actual and actual != self._region:
                    logger.info(
                        "Bucket %s lives in %s (client was pointed at %s); reconfiguring.",
                        self._bucket,
                        actual,
                        self._region,
                    )
                    self._reconfigure_for_region(actual)
                    self._region_verified = True
                    return
                # Sticky failure: cache once and re-raise the same helpful
                # message on every subsequent call. Avoids both the repeated
                # HEAD cost and the inconsistent (helpful-on-call-1, raw-on-
                # call-2+) error the previous implementation produced.
                err = StorageError(
                    f"Bucket {self._bucket!r} preflight failed: {exc}. "
                    "Check bucket name, region, and credentials."
                )
                self._preflight_error = err
                self._region_verified = True
                raise err from exc

    def _reconfigure_for_region(self, region: str) -> None:
        """Rebuild the boto3 client for a different B2 region.

        Called only for B2 endpoints (see ``_is_b2``) — synthesizes the
        correct B2 regional endpoint URL. Credentials come from the
        instance (stored in ``__init__``) since ``boto3.client.meta.config``
        does not hold them.
        """
        import boto3

        self._region = region
        self._endpoint_url = f"https://s3.{region}.backblazeb2.com"
        self._client = boto3.client("s3", **self._client_kwargs())
        self._transfer_config = self._build_transfer_config()

    def ensure_lifecycle_defaults(
        self,
        *,
        cancel_multipart_after_days: int = _DEFAULT_CANCEL_MULTIPART_DAYS,
        noncurrent_version_expire_days: int | None = _DEFAULT_NONCURRENT_EXPIRE_DAYS,
    ) -> None:
        """Apply idempotent lifecycle rules tuned for a genblaze workload.

        Orphaned multipart uploads from failed video transfers otherwise
        accumulate billable storage forever. Noncurrent-version expiry
        tidies the per-run manifest history that B2's always-on versioning
        creates when manifests are rewritten.

        Pass ``noncurrent_version_expire_days=None`` to keep all manifest
        versions forever (full provenance history, higher storage cost).
        """
        rules: list[dict[str, Any]] = [
            {
                "ID": "genblaze-cancel-unfinished-multipart",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": cancel_multipart_after_days
                },
            }
        ]
        if noncurrent_version_expire_days is not None:
            rules.append(
                {
                    "ID": "genblaze-expire-noncurrent-versions",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "NoncurrentVersionExpiration": {
                        "NoncurrentDays": noncurrent_version_expire_days
                    },
                }
            )
        try:
            self._client.put_bucket_lifecycle_configuration(
                Bucket=self._bucket,
                LifecycleConfiguration={"Rules": rules},
            )
            logger.info("Applied %d lifecycle rule(s) to bucket %s", len(rules), self._bucket)
        except Exception as exc:
            # Non-fatal — users with read-only keys or managed infra should
            # still be able to upload. Log and continue.
            logger.warning("Failed to apply lifecycle defaults to %s: %s", self._bucket, exc)

    @classmethod
    def for_backblaze(
        cls,
        bucket: str | None = None,
        *,
        region: str | None = None,
        key_id: str | None = None,
        app_key: str | None = None,
        public_url_base: str | None = None,
        auto_lifecycle: bool = True,
    ) -> S3StorageBackend:
        """Construct an S3StorageBackend preconfigured for Backblaze B2.

        Derives B2's S3 endpoint from ``region`` and falls back to the
        ``B2_BUCKET`` / ``B2_REGION`` / ``B2_KEY_ID`` / ``B2_APP_KEY``
        environment variables when arguments are not passed explicitly.
        Raises ``ValueError`` if bucket or credentials are missing entirely
        — prefer a clear error at construction over a cryptic
        ``NoCredentialsError`` mid-upload.

        The first ``put()`` / ``exists()`` call auto-detects the bucket's
        actual region when B2 returns a redirect; passing ``region=`` (or
        setting ``$B2_REGION``) is an optimization hint. Regions that reject
        cross-region requests with 403 instead of 301 (e.g. ``us-east-005``)
        must still be specified — auto-detect can't read a header that isn't
        sent.

        Args:
            bucket: B2 bucket name. Defaults to ``$B2_BUCKET``.
            region: B2 region slug (e.g. "us-west-004", "us-east-005",
                "eu-central-003"). Defaults to ``$B2_REGION``, then
                ``"us-west-004"``. Auto-corrected on first use if B2
                returns a redirect.
            key_id: B2 application key ID. Defaults to ``$B2_KEY_ID``.
            app_key: B2 application key. Defaults to ``$B2_APP_KEY``.
            public_url_base: Optional B2 friendly-URL base for public buckets,
                e.g. ``"https://f004.backblazeb2.com/file/my-bucket"``. When
                set, :meth:`get_url` returns these instead of pre-signed URLs.
            auto_lifecycle: When True (default), apply recommended lifecycle
                rules on construction — cancel orphaned multipart uploads
                after 7 days and expire noncurrent manifest versions after
                30 days. Set False if lifecycle is managed out-of-band
                (Terraform, console, IaC).

        Example::

            # All config from environment (B2_BUCKET, B2_REGION, B2_KEY_ID, B2_APP_KEY)
            backend = S3StorageBackend.for_backblaze()
            # Or pass explicitly
            backend = S3StorageBackend.for_backblaze("my-bucket", region="us-east-005")
            sink = ObjectStorageSink(backend, key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        """
        resolved_bucket = bucket or os.environ.get("B2_BUCKET")
        if not resolved_bucket:
            raise ValueError(
                "Backblaze B2 bucket missing. Set B2_BUCKET environment "
                "variable, or pass bucket= explicitly to for_backblaze()."
            )
        resolved_region = region or os.environ.get("B2_REGION") or "us-west-004"
        resolved_key = key_id or os.environ.get("B2_KEY_ID")
        resolved_secret = app_key or os.environ.get("B2_APP_KEY")
        if not resolved_key or not resolved_secret:
            raise ValueError(
                "Backblaze B2 credentials missing. Set B2_KEY_ID / B2_APP_KEY "
                "environment variables, or pass key_id= and app_key= "
                "explicitly to for_backblaze()."
            )
        backend = cls(
            bucket=resolved_bucket,
            endpoint_url=f"https://s3.{resolved_region}.backblazeb2.com",
            region=resolved_region,
            public_url_base=public_url_base,
            aws_access_key_id=resolved_key,
            aws_secret_access_key=resolved_secret,
        )
        if auto_lifecycle:
            # Region may still be wrong here — ensure_lifecycle_defaults calls
            # put_bucket_lifecycle_configuration which surfaces 301 cleanly
            # via the normal region-redirect path. Verify first so the
            # lifecycle call lands on the right region.
            try:
                backend._ensure_region_verified()
            except StorageError as exc:
                logger.warning("auto_lifecycle skipped — bucket preflight failed: %s", exc)
                return backend
            backend.ensure_lifecycle_defaults()
        return backend
