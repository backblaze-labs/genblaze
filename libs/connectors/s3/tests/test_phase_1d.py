"""Phase 1D regression tests — URLPolicy / Encryption wiring + presigned methods.

Closes bugs #2 (silent ``expires_in`` precedence under ``public_url_base``),
#3 (asymmetric SSE-C between ``put`` and ``get``/``copy``), and #7 (HeadBucket
on every public URL render). Also pins the new ``presigned_get`` /
``presigned_put`` API contracts that ship with this phase.
"""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import StorageError
from genblaze_s3 import Encryption, PresignedURL, URLPolicy, URLPolicyError

from tests.conftest import _FakeClientError

# 32-byte AES-256 key fixture for SSE-C tests.
_KEY = bytes(range(32))
_KEY_MD5 = base64.b64encode(hashlib.md5(_KEY).digest()).decode("ascii")  # noqa: S324


def _make_backend(mock_boto3_mod, **kwargs):
    """Construct a region-verified backend with a fresh mock client."""
    from genblaze_s3.backend import S3StorageBackend

    mock_client = MagicMock()
    mock_boto3_mod.client.return_value = mock_client
    defaults = {
        "bucket": "my-bucket",
        "endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "region": "us-west-004",
    }
    defaults.update(kwargs)
    backend = S3StorageBackend(**defaults)
    backend._region_verified = True  # skip preflight
    return backend, mock_client


# ---------------------------------------------------------------------------
# URLPolicy wiring (bug #2 + bug #7)
# ---------------------------------------------------------------------------


class TestURLPolicy:
    def test_auto_with_public_base_returns_public_url(self, mock_boto3):
        backend, mock_client = _make_backend(
            mock_boto3, public_url_base="https://cdn.example.com/file"
        )
        url = backend.get_url("img.png")
        assert url == "https://cdn.example.com/file/img.png"
        # Bug #7: no HeadBucket / no presigning on the public path.
        mock_client.head_bucket.assert_not_called()
        mock_client.generate_presigned_url.assert_not_called()

    def test_auto_without_public_base_returns_presigned(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed/abc"
        url = backend.get_url("k")
        assert url == "https://signed/abc"
        mock_client.generate_presigned_url.assert_called_once()

    def test_public_policy_without_public_base_raises(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(URLPolicyError, match="public_url_base"):
            backend.get_url("k", policy=URLPolicy.PUBLIC)

    def test_public_policy_with_explicit_expires_in_raises(self, mock_boto3):
        """The fix for bug #2 — explicit ``expires_in`` while requesting a
        public URL is now a typed error instead of a silent ignore."""
        backend, _ = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        with pytest.raises(URLPolicyError, match="does not honor expires_in"):
            backend.get_url("k", policy=URLPolicy.PUBLIC, expires_in=600)

    def test_public_policy_without_explicit_expires_in_works(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        # No expires_in passed → no conflict.
        url = backend.get_url("k", policy=URLPolicy.PUBLIC)
        assert url == "https://cdn.example.com/k"

    def test_presigned_policy_overrides_public_base(self, mock_boto3):
        """Even with ``public_url_base`` configured, an explicit
        PRESIGNED policy returns a SigV4 URL — ``expires_in`` honored."""
        backend, mock_client = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        mock_client.generate_presigned_url.return_value = "https://signed/k"
        url = backend.get_url("k", policy=URLPolicy.PRESIGNED, expires_in=900)
        assert url == "https://signed/k"
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "my-bucket", "Key": "k"},
            ExpiresIn=900,
        )

    def test_auto_public_branch_skips_head_bucket_on_unverified_backend(self, mock_boto3):
        """**Bug #7 fix:** the public-URL render path no longer triggers
        a region-verify HeadBucket. Useful for offline dev where the
        bucket isn't reachable but the URL shape is still meaningful."""
        backend, mock_client = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        # Reset to unverified so any HeadBucket call would surface.
        backend._region_verified = False
        backend.get_url("k")
        mock_client.head_bucket.assert_not_called()


# ---------------------------------------------------------------------------
# Encryption wiring (bug #3)
# ---------------------------------------------------------------------------


class TestEncryptionOnPut:
    def test_sse_s3(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        backend.put("k", b"data", encryption=Encryption.sse_s3())
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["ServerSideEncryption"] == "AES256"

    def test_sse_kms(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        backend.put("k", b"data", encryption=Encryption.sse_kms("alias/x"))
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["ServerSideEncryption"] == "aws:kms"
        assert extra["SSEKMSKeyId"] == "alias/x"

    def test_sse_c(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        backend.put("k", b"data", encryption=Encryption.sse_c(_KEY))
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["SSECustomerAlgorithm"] == "AES256"
        assert extra["SSECustomerKey"] == _KEY
        assert extra["SSECustomerKeyMD5"] == _KEY_MD5

    def test_sse_overlap_with_extra_args_raises(self, mock_boto3):
        """SSE envelope conflict between ``encryption=`` and overlapping
        ``extra_args`` keys raises rather than silently encrypting with
        the wrong material. Picks exactly one source of truth.

        Pre-fix: ``extra_args`` overrode the value object piecewise,
        producing mismatched envelopes (wrong KMS key, partial
        customer-key state) the API silently accepted.
        """
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(ValueError, match="SSE envelope conflict"):
            backend.put(
                "k",
                b"data",
                encryption=Encryption.sse_s3(),
                extra_args={"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "alias/y"},
            )

    def test_non_sse_extra_args_alongside_encryption_works(self, mock_boto3):
        """Non-SSE ``extra_args`` keys still compose with ``encryption=``."""
        backend, mock_client = _make_backend(mock_boto3)
        backend.put(
            "k",
            b"data",
            encryption=Encryption.sse_s3(),
            extra_args={"CacheControl": "public, max-age=3600"},
        )
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        # Encryption value object set ServerSideEncryption.
        assert extra["ServerSideEncryption"] == "AES256"
        # Caller's non-SSE key composes alongside.
        assert extra["CacheControl"] == "public, max-age=3600"


class TestEncryptionOnGet:
    def test_sse_c_passes_customer_key_to_get_object(self, mock_boto3):
        """Closes the bug #3 read-side asymmetry — SSE-C reads need the
        same customer-key envelope as the write."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b"plaintext"
        mock_client.get_object.return_value = {"Body": body}
        out = backend.get("k", encryption=Encryption.sse_c(_KEY))
        assert out == b"plaintext"
        kwargs = mock_client.get_object.call_args.kwargs
        assert kwargs["SSECustomerAlgorithm"] == "AES256"
        assert kwargs["SSECustomerKey"] == _KEY
        assert kwargs["SSECustomerKeyMD5"] == _KEY_MD5
        # Bucket/Key still present.
        assert kwargs["Bucket"] == "my-bucket"
        assert kwargs["Key"] == "k"

    def test_sse_s3_get_no_extra_kwargs(self, mock_boto3):
        """Server-managed key: read path needs nothing extra."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b""
        mock_client.get_object.return_value = {"Body": body}
        backend.get("k", encryption=Encryption.sse_s3())
        kwargs = mock_client.get_object.call_args.kwargs
        # Only Bucket + Key — no SSECustomer* keys for SSE-S3.
        assert "SSECustomerAlgorithm" not in kwargs

    def test_get_without_encryption_unchanged(self, mock_boto3):
        """Existing call sites passing no encryption= continue to work."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b""
        mock_client.get_object.return_value = {"Body": body}
        backend.get("k")
        mock_client.get_object.assert_called_once_with(Bucket="my-bucket", Key="k")


class TestEncryptionOnCopy:
    def test_sse_c_copy_passes_source_and_dest(self, mock_boto3):
        """SSE-C copy must carry both ``CopySourceSSECustomerKey`` (read
        side) and ``SSECustomerKey`` (write side); both come from one
        :meth:`Encryption.to_copy_extra_args` call."""
        backend, mock_client = _make_backend(mock_boto3)
        backend.copy("src", "dst", encryption=Encryption.sse_c(_KEY))
        kwargs = mock_client.copy_object.call_args.kwargs
        assert kwargs["CopySourceSSECustomerKey"] == _KEY
        assert kwargs["SSECustomerKey"] == _KEY
        assert kwargs["Bucket"] == "my-bucket"
        assert kwargs["Key"] == "dst"
        assert kwargs["CopySource"] == {"Bucket": "my-bucket", "Key": "src"}

    def test_copy_without_encryption_unchanged(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        backend.copy("src", "dst")
        kwargs = mock_client.copy_object.call_args.kwargs
        # No encryption-shaped keys at all.
        assert "CopySourceSSECustomerKey" not in kwargs
        assert "SSECustomerKey" not in kwargs


# ---------------------------------------------------------------------------
# presigned_get / presigned_put — bug #1 follow-through
# ---------------------------------------------------------------------------


class TestPresignedGet:
    def test_returns_presigned_url_value_object(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = (
            "https://example.s3.amazonaws.com/k?X-Amz-Signature=abcdef&X-Amz-Credential=AKIA"
        )
        result = backend.presigned_get("k")
        assert isinstance(result, PresignedURL)
        assert result.method == "GET"
        assert result.key == "k"
        assert result.bucket == "my-bucket"
        assert result.expires_in == 3600
        # Boto invocation shape.
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "my-bucket", "Key": "k"},
            ExpiresIn=3600,
        )

    def test_repr_redacts_signature(self, mock_boto3):
        """Phase 1A's ``PresignedURL`` redaction kicks in here — the
        whole reason ``presigned_get`` exists."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = (
            "https://example.s3.amazonaws.com/k?X-Amz-Signature=secret-sig"
        )
        result = backend.presigned_get("k")
        assert "secret-sig" not in repr(result)
        assert "secret-sig" not in str(result)
        # Underlying URL still accessible explicitly.
        assert "secret-sig" in result.url

    def test_custom_expires_in(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed/x"
        result = backend.presigned_get("k", expires_in=900)
        assert result.expires_in == 900
        kwargs = mock_client.generate_presigned_url.call_args.kwargs
        assert kwargs["ExpiresIn"] == 900

    def test_botocore_error_wrapped_as_storage_error(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.side_effect = _FakeClientError(
            {"Error": {"Code": "AccessDenied"}}, "GeneratePresignedUrl"
        )
        with pytest.raises(StorageError, match="presigned_get"):
            backend.presigned_get("k")


class TestStickyPreflightCacheFilter:
    """Phase 1 review fix #3: transient errors don't permanently brick the backend.

    Pre-fix: any non-redirect ClientError at preflight cached
    ``_preflight_error`` permanently. A transient 503 at construction
    bricked the backend forever. Post-fix: only non-retriable errors
    (auth, missing bucket) are sticky; 5xx/throttle/network re-raise
    without caching so the next call retries.
    """

    def test_transient_503_does_not_cache(self, mock_boto3):
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        # First call: synthetic 503. Second call: success.
        mock_client.head_bucket.side_effect = [
            _FakeClientError(
                {"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}},
                "HeadBucket",
            ),
            None,  # success on retry
        ]
        backend = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        # First call raises but does NOT mark the backend as verified.
        with pytest.raises(StorageError, match="preflight failed"):
            backend._ensure_region_verified()
        assert backend._region_verified is False
        assert backend._preflight_error is None
        # Second call retries the head_bucket and succeeds.
        backend._ensure_region_verified()
        assert backend._region_verified is True
        assert mock_client.head_bucket.call_count == 2

    def test_auth_failure_caches_permanently(self, mock_boto3):
        """Bad credentials are sticky — same error on every subsequent call,
        no repeated HeadBucket round-trips."""
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.head_bucket.side_effect = _FakeClientError(
            {
                "Error": {"Code": "SignatureDoesNotMatch"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "HeadBucket",
        )
        backend = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        with pytest.raises(StorageError, match="preflight failed"):
            backend._ensure_region_verified()
        # Second call also raises — but no second HeadBucket round-trip.
        with pytest.raises(StorageError, match="preflight failed"):
            backend._ensure_region_verified()
        assert mock_client.head_bucket.call_count == 1

    def test_missing_bucket_caches_permanently(self, mock_boto3):
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.head_bucket.side_effect = _FakeClientError(
            {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadBucket",
        )
        backend = S3StorageBackend(
            bucket="missing",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        with pytest.raises(StorageError):
            backend._ensure_region_verified()
        with pytest.raises(StorageError):
            backend._ensure_region_verified()
        assert mock_client.head_bucket.call_count == 1


class TestAsyncGetUrlForwardsKwargs:
    """Phase 1 review fix #1: aget_url forwards policy= and uses None
    sentinel so the URLPolicy.PUBLIC conflict detection works on the
    async path the same way it does on sync."""

    def test_aget_url_no_args_uses_default_sentinel(self, mock_boto3):
        """Default expires_in=None means "don't pass" — sync get_url sees
        the unset sentinel and behaves as if no expires_in was given."""
        import asyncio

        backend, mock_client = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        url = asyncio.run(backend.aget_url("k"))
        assert url == "https://cdn.example.com/k"
        # No HeadBucket on the public path (bug #7).
        mock_client.head_bucket.assert_not_called()

    def test_aget_url_forwards_policy_kwarg(self, mock_boto3):
        """policy= reaches the sync impl via **kwargs forwarding."""
        import asyncio

        backend, mock_client = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        mock_client.generate_presigned_url.return_value = "https://signed/k"
        url = asyncio.run(backend.aget_url("k", policy=URLPolicy.PRESIGNED))
        assert url == "https://signed/k"

    def test_aget_url_public_policy_with_expires_in_raises(self, mock_boto3):
        """Async path now exposes the same conflict detection as sync."""
        import asyncio

        backend, _ = _make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        with pytest.raises(URLPolicyError):
            asyncio.run(backend.aget_url("k", policy=URLPolicy.PUBLIC, expires_in=600))


class TestPresignedPut:
    def test_basic(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed/upload"
        result = backend.presigned_put("k")
        assert isinstance(result, PresignedURL)
        assert result.method == "PUT"
        assert result.key == "k"
        assert result.expires_in == 3600
        kwargs = mock_client.generate_presigned_url.call_args
        assert kwargs.args[0] == "put_object"
        assert kwargs.kwargs["Params"] == {"Bucket": "my-bucket", "Key": "k"}
        assert kwargs.kwargs["ExpiresIn"] == 3600

    def test_with_content_type_binds_header_into_signature(self, mock_boto3):
        """Pinning ``content_type`` via ``Params`` makes SigV4 require
        the same header on upload — pass-through verifies the kwarg
        shape boto3 expects."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed/upload"
        result = backend.presigned_put("k", expires_in=600, content_type="image/png")
        assert result.expires_in == 600
        params = mock_client.generate_presigned_url.call_args.kwargs["Params"]
        assert params["ContentType"] == "image/png"

    def test_botocore_error_wrapped_as_storage_error(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.generate_presigned_url.side_effect = _FakeClientError(
            {"Error": {"Code": "AccessDenied"}}, "GeneratePresignedUrl"
        )
        with pytest.raises(StorageError, match="presigned_put"):
            backend.presigned_put("k")
