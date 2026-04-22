"""S3StorageBackend — works with any S3-compatible service (B2, R2, MinIO, AWS)."""

from __future__ import annotations

import io
import logging
import os
from typing import TYPE_CHECKING, Any, BinaryIO

from genblaze_core._version import __version__
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.base import StorageBackend

try:
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover — botocore ships with boto3
    ClientError = Exception  # type: ignore[assignment,misc]

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
        """Check if an object exists in S3."""
        try:
            self._ensure_region_verified()
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
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

    def get_url(self, key: str, *, expires_in: int = 3600) -> str:
        """Get URL for an object. Uses public_url_base if configured, else pre-signed."""
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

    def _ensure_region_verified(self) -> None:
        """Lazy-verify the bucket region on first use; follow redirect if wrong.

        B2 (and AWS) return 301 ``PermanentRedirect`` with an
        ``x-amz-bucket-region`` header when the client is pointed at the
        wrong region. For B2 endpoints we rewrite to the correct B2 regional
        endpoint — users stop needing to hand-pick the right ``region=`` on
        ``for_backblaze``. For other endpoints (AWS S3, R2, MinIO) we don't
        attempt to rebuild the endpoint URL since we can't synthesize it;
        we just surface the error with guidance.
        """
        if self._region_verified:
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
            # Mark verified even on non-redirect errors so we don't re-HEAD
            # on every subsequent call. The error will recur on the real
            # operation (get/put/etc.) and be surfaced consistently there.
            self._region_verified = True
            raise StorageError(
                f"Bucket {self._bucket!r} preflight failed: {exc}. "
                "Check bucket name, region, and credentials."
            ) from exc

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
        bucket: str,
        *,
        region: str = "us-west-004",
        key_id: str | None = None,
        app_key: str | None = None,
        public_url_base: str | None = None,
        auto_lifecycle: bool = True,
    ) -> S3StorageBackend:
        """Construct an S3StorageBackend preconfigured for Backblaze B2.

        Derives B2's S3 endpoint from ``region`` and falls back to the
        ``B2_KEY_ID`` / ``B2_APP_KEY`` environment variables when
        credentials are not passed explicitly. Raises ``ValueError`` if
        credentials are missing entirely — prefer a clear error at
        construction over a cryptic ``NoCredentialsError`` mid-upload.

        The first ``put()`` / ``exists()`` call auto-detects the bucket's
        actual region; passing ``region=`` is an optimization hint, not a
        requirement. If the bucket lives elsewhere the backend transparently
        reconfigures itself.

        Args:
            bucket: B2 bucket name.
            region: B2 region slug hint (e.g. "us-west-004", "eu-central-003").
                Auto-corrected on first use if the bucket lives in a different
                region.
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

            backend = S3StorageBackend.for_backblaze("my-bucket")
            sink = ObjectStorageSink(backend, key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        """
        resolved_key = key_id or os.environ.get("B2_KEY_ID")
        resolved_secret = app_key or os.environ.get("B2_APP_KEY")
        if not resolved_key or not resolved_secret:
            raise ValueError(
                "Backblaze B2 credentials missing. Set B2_KEY_ID / B2_APP_KEY "
                "environment variables, or pass key_id= and app_key= "
                "explicitly to for_backblaze()."
            )
        backend = cls(
            bucket=bucket,
            endpoint_url=f"https://s3.{region}.backblazeb2.com",
            region=region,
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
