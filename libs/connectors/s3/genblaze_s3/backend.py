"""S3StorageBackend — works with any S3-compatible service (B2, R2, MinIO, AWS)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, BinaryIO

from genblaze_core._version import __version__
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.base import StorageBackend

if TYPE_CHECKING:
    pass

logger = logging.getLogger("genblaze.s3")

# User agent for B2/S3 API tracking
_USER_AGENT = f"b2ai-genblaze/{__version__}"


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

        # Lazy import boto3 (same pattern as replicate connector)
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for S3StorageBackend. "
                'Install it with: pip install "genblaze-s3"'
            ) from exc

        kwargs: dict = {}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region:
            kwargs["region_name"] = region
        if aws_access_key_id:
            kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            kwargs["aws_secret_access_key"] = aws_secret_access_key

        # User agent for B2 attribution; adaptive retries for transient 429/503s;
        # explicit timeouts because boto3's 60s default read_timeout can fire
        # mid-upload on slow links with GB-sized video payloads.
        kwargs["config"] = BotoConfig(
            user_agent_extra=_USER_AGENT,
            retries={"max_attempts": 3, "mode": "adaptive"},
            connect_timeout=30,
            read_timeout=300,
        )

        self._client = boto3.client("s3", **kwargs)

    def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Upload an object to S3. Returns the storage URL."""
        try:
            kwargs: dict = {"Bucket": self._bucket, "Key": key, "Body": data}
            if content_type:
                kwargs["ContentType"] = content_type
            if metadata:
                kwargs["Metadata"] = metadata
            self._client.put_object(**kwargs)
            return self.get_url(key)
        except Exception as exc:
            raise StorageError(f"S3 put failed for {key}: {exc}") from exc

    def get(self, key: str) -> bytes:
        """Download an object from S3."""
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except Exception as exc:
            raise StorageError(f"S3 get failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        """Check if an object exists in S3."""
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                return False
            raise StorageError(f"S3 exists check failed for {key}: {exc}") from exc

    def delete(self, key: str) -> None:
        """Delete an object from S3."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"S3 delete failed for {key}: {exc}") from exc

    def get_url(self, key: str, *, expires_in: int = 3600) -> str:
        """Get URL for an object. Uses public_url_base if configured, else pre-signed."""
        if self._public_url_base:
            from urllib.parse import quote

            return f"{self._public_url_base}/{quote(key, safe='/')}"
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:
            raise StorageError(f"S3 get_url failed for {key}: {exc}") from exc

    # close() intentionally not overridden — base class no-op is correct.
    # boto3 clients don't have a close() method; calling it would raise.

    @classmethod
    def for_backblaze(
        cls,
        bucket: str,
        *,
        region: str = "us-west-004",
        key_id: str | None = None,
        app_key: str | None = None,
        public_url_base: str | None = None,
    ) -> S3StorageBackend:
        """Construct an S3StorageBackend preconfigured for Backblaze B2.

        Derives B2's S3 endpoint from ``region`` and falls back to the
        ``B2_KEY_ID`` / ``B2_APP_KEY`` environment variables when
        credentials are not passed explicitly.

        Args:
            bucket: B2 bucket name.
            region: B2 region slug (e.g. "us-west-004", "eu-central-003").
            key_id: B2 application key ID. Defaults to ``$B2_KEY_ID``.
            app_key: B2 application key. Defaults to ``$B2_APP_KEY``.
            public_url_base: Optional B2 friendly-URL base for public buckets,
                e.g. ``"https://f004.backblazeb2.com/file/my-bucket"``. When
                set, :meth:`get_url` returns these instead of pre-signed URLs.

        Example::

            backend = S3StorageBackend.for_backblaze("my-bucket")
            sink = ObjectStorageSink(backend, key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        """
        return cls(
            bucket=bucket,
            endpoint_url=f"https://s3.{region}.backblazeb2.com",
            region=region,
            public_url_base=public_url_base,
            aws_access_key_id=key_id or os.environ.get("B2_KEY_ID"),
            aws_secret_access_key=app_key or os.environ.get("B2_APP_KEY"),
        )
