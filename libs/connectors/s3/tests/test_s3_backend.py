"""Tests for S3StorageBackend — uses mock boto3."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


class _FakeClientError(Exception):
    """Real exception subclass so backend code can `except ClientError` as usual."""

    def __init__(self, response, operation_name):
        super().__init__(operation_name)
        self.response = response
        self.operation_name = operation_name


@pytest.fixture(autouse=True)
def mock_boto3():
    """Mock boto3 module before importing S3StorageBackend."""
    mock_mod = MagicMock()
    mock_botocore = MagicMock()
    # botocore.exceptions.ClientError must be a real exception class — the
    # backend does `except ClientError`, which requires a real type.
    mock_botocore.exceptions.ClientError = _FakeClientError
    modules = {
        "boto3": mock_mod,
        # boto3.s3.transfer.TransferConfig is imported at backend init time;
        # exposing the submodule path satisfies the `from ... import` form.
        "boto3.s3": mock_mod.s3,
        "boto3.s3.transfer": mock_mod.s3.transfer,
        "botocore": mock_botocore,
        "botocore.config": mock_botocore.config,
        "botocore.exceptions": mock_botocore.exceptions,
    }
    with patch.dict(sys.modules, modules):
        yield mock_mod


class TestS3StorageBackend:
    def _make_backend(self, mock_boto3_mod, **kwargs):
        # Re-import to pick up the mocked boto3
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
        # Skip the head_bucket preflight for most tests — covered separately.
        backend._region_verified = True
        return backend, mock_client

    def test_put_uses_upload_fileobj(self, mock_boto3):
        """put() routes through upload_fileobj so small+large payloads share the
        managed-transfer code path (auto-multipart when > threshold)."""
        backend, mock_client = self._make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed/key"
        backend.put("test/key.png", b"data", content_type="image/png")
        mock_client.upload_fileobj.assert_called_once()
        call_kwargs = mock_client.upload_fileobj.call_args.kwargs
        args = mock_client.upload_fileobj.call_args.args
        # Positional args: (stream, bucket, key)
        assert args[1] == "my-bucket"
        assert args[2] == "test/key.png"
        extra = call_kwargs["ExtraArgs"]
        assert extra["ContentType"] == "image/png"
        # SHA-256 per-part integrity is the default unless overridden.
        assert extra["ChecksumAlgorithm"] == "SHA256"
        # put_object is no longer used for the body path.
        mock_client.put_object.assert_not_called()

    def test_put_passes_extra_args(self, mock_boto3):
        """extra_args passthrough lets callers set Cache-Control, SSE, etc."""
        backend, mock_client = self._make_backend(mock_boto3)
        backend.put(
            "k",
            b"data",
            extra_args={"CacheControl": "public, max-age=31536000, immutable"},
        )
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["CacheControl"] == "public, max-age=31536000, immutable"

    def test_put_caller_checksum_override(self, mock_boto3):
        """Explicit ChecksumSHA256 routes through put_object (single-PUT).

        Whole-object SHA-256 checksums are only valid on single-part
        uploads; upload_fileobj's multipart path would either ignore or
        reject them. See TestPutExplicitChecksumRoutes below for the full
        routing contract.
        """
        backend, mock_client = self._make_backend(mock_boto3)
        backend.put("k", b"data", extra_args={"ChecksumSHA256": "base64hash=="})
        mock_client.put_object.assert_called_once()
        mock_client.upload_fileobj.assert_not_called()
        kwargs = mock_client.put_object.call_args.kwargs
        assert kwargs["ChecksumSHA256"] == "base64hash=="
        # When caller passes explicit checksum, don't also pin the algorithm.
        assert "ChecksumAlgorithm" not in kwargs

    def test_put_binaryio_passes_through(self, mock_boto3):
        """Streaming inputs go straight to upload_fileobj without BytesIO wrapping."""
        import io

        backend, mock_client = self._make_backend(mock_boto3)
        stream = io.BytesIO(b"payload")
        backend.put("k", stream)
        # First positional arg should be the stream we passed (identity).
        args = mock_client.upload_fileobj.call_args.args
        assert args[0] is stream

    def test_get(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        mock_body = MagicMock()
        mock_body.read.return_value = b"file content"
        mock_client.get_object.return_value = {"Body": mock_body}
        data = backend.get("test/key.png")
        assert data == b"file content"
        mock_client.get_object.assert_called_once_with(Bucket="my-bucket", Key="test/key.png")

    def test_delete(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        backend.delete("test/key.png")
        mock_client.delete_object.assert_called_once_with(Bucket="my-bucket", Key="test/key.png")

    def test_get_url_with_public_base(self, mock_boto3):
        """When public_url_base is set, get_url returns friendly URL."""
        backend, _ = self._make_backend(mock_boto3, public_url_base="https://cdn.example.com/file")
        url = backend.get_url("assets/img.png")
        assert url == "https://cdn.example.com/file/assets/img.png"

    def test_get_url_public_encodes_special_chars(self, mock_boto3):
        """Keys with spaces/special chars must be percent-encoded in public URLs."""
        backend, _ = self._make_backend(mock_boto3, public_url_base="https://cdn.example.com")
        url = backend.get_url("assets/my file (1).png")
        assert url == "https://cdn.example.com/assets/my%20file%20%281%29.png"

    def test_get_url_presigned(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed-url"
        url = backend.get_url("key", expires_in=600)
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "my-bucket", "Key": "key"},
            ExpiresIn=600,
        )
        assert url == "https://signed-url"

    def test_close_noop(self, mock_boto3):
        """close() is a no-op — boto3 clients don't have a close() method."""
        backend, mock_client = self._make_backend(mock_boto3)
        backend.close()  # Should not raise
        mock_client.close.assert_not_called()


class TestRegionPreflight:
    """head_bucket-based region auto-detection happens once on first use."""

    def test_happy_path_verifies_once(self, mock_boto3):
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        backend = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        backend.put("k", b"d")
        backend.put("k2", b"d")
        # head_bucket fired only on the first upload.
        assert mock_client.head_bucket.call_count == 1

    def test_wrong_region_redirects_and_reconfigures(self, mock_boto3):
        from genblaze_s3.backend import S3StorageBackend

        mock_client_1 = MagicMock()
        mock_client_2 = MagicMock()
        # Client 1 (wrong region): head_bucket raises ClientError with the
        # real region in ResponseMetadata headers. Client 2 (after reconfig):
        # normal uploads succeed.
        mock_boto3.client.side_effect = [mock_client_1, mock_client_2]
        err = _FakeClientError(
            {
                "Error": {"Code": "PermanentRedirect"},
                "ResponseMetadata": {"HTTPHeaders": {"x-amz-bucket-region": "eu-central-003"}},
            },
            "HeadBucket",
        )
        mock_client_1.head_bucket.side_effect = err

        backend = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
            aws_access_key_id="the-key",
            aws_secret_access_key="the-secret",
        )
        backend.put("k", b"d")
        assert backend._region == "eu-central-003"
        # The reconfigured client is what ran upload_fileobj.
        mock_client_2.upload_fileobj.assert_called_once()
        # Regression: credentials must survive the reconfigure. Before the
        # fix, we tried to read creds from client.meta.config.__dict__
        # (which doesn't hold them) and the reconfigured client silently
        # dropped credentials, leading to NoCredentialsError mid-upload.
        reconfigure_kwargs = mock_boto3.client.call_args_list[1].kwargs
        assert reconfigure_kwargs["aws_access_key_id"] == "the-key"
        assert reconfigure_kwargs["aws_secret_access_key"] == "the-secret"  # noqa: S105 — test fixture
        assert reconfigure_kwargs["endpoint_url"] == "https://s3.eu-central-003.backblazeb2.com"

    def test_non_b2_endpoint_does_not_rewrite_on_redirect(self, mock_boto3):
        """AWS S3 / R2 / MinIO endpoints must not be rewritten as B2 URLs.

        Regression: the initial implementation rewrote endpoint_url to
        https://s3.{region}.backblazeb2.com on any 301 PermanentRedirect,
        which would have silently retargeted AWS S3 users at B2.
        """
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.head_bucket.side_effect = _FakeClientError(
            {
                "Error": {"Code": "PermanentRedirect"},
                "ResponseMetadata": {"HTTPHeaders": {"x-amz-bucket-region": "eu-west-1"}},
            },
            "HeadBucket",
        )
        # Plain AWS S3 — no endpoint_url.
        backend = S3StorageBackend(bucket="b")
        with pytest.raises(Exception):  # noqa: B017 — any exception shape is acceptable
            backend.put("k", b"d")
        # boto3.client must have been called exactly once (no reconfigure).
        assert mock_boto3.client.call_count == 1


class TestRegionPreflightOnAllMethods:
    """get/exists/delete/get_url all preflight the region on first use."""

    def _backend_with_unverified_region(self, mock_boto3_mod):
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client
        backend = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        # Leave _region_verified=False so the preflight must fire.
        return backend, mock_client

    def test_exists_preflights(self, mock_boto3):
        backend, mock_client = self._backend_with_unverified_region(mock_boto3)
        backend.exists("k")
        mock_client.head_bucket.assert_called_once()

    def test_get_preflights(self, mock_boto3):
        backend, mock_client = self._backend_with_unverified_region(mock_boto3)
        mock_client.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b""))}
        backend.get("k")
        mock_client.head_bucket.assert_called_once()

    def test_delete_preflights(self, mock_boto3):
        backend, mock_client = self._backend_with_unverified_region(mock_boto3)
        backend.delete("k")
        mock_client.head_bucket.assert_called_once()

    def test_get_url_presigned_preflights(self, mock_boto3):
        backend, mock_client = self._backend_with_unverified_region(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://s/k"
        backend.get_url("k")
        mock_client.head_bucket.assert_called_once()

    def test_get_url_public_skips_preflight(self, mock_boto3):
        """Public-URL mode doesn't hit the wire — no need to preflight."""
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        backend = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            public_url_base="https://cdn.example.com",
        )
        backend.get_url("k")
        mock_client.head_bucket.assert_not_called()


class TestPutExplicitChecksumRoutes:
    """Explicit ChecksumSHA256 must go through put_object (single-PUT)."""

    def _make_backend(self, mock_boto3_mod):
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client
        backend = S3StorageBackend(
            bucket="b", endpoint_url="https://s3.us-west-004.backblazeb2.com"
        )
        backend._region_verified = True
        return backend, mock_client

    def test_explicit_checksum_uses_put_object(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        backend.put("k", b"d", extra_args={"ChecksumSHA256": "abc123=="})
        mock_client.put_object.assert_called_once()
        mock_client.upload_fileobj.assert_not_called()
        kwargs = mock_client.put_object.call_args.kwargs
        assert kwargs["ChecksumSHA256"] == "abc123=="

    def test_default_still_uses_upload_fileobj(self, mock_boto3):
        """Regression guard — the default path must stay on multipart-capable
        upload_fileobj. Only the explicit-whole-object-checksum escape hatch
        falls through to put_object."""
        backend, mock_client = self._make_backend(mock_boto3)
        backend.put("k", b"d")
        mock_client.upload_fileobj.assert_called_once()
        mock_client.put_object.assert_not_called()


class TestLifecycleDefaults:
    """ensure_lifecycle_defaults applies idempotent bucket lifecycle rules."""

    def _make_backend(self, mock_boto3_mod):
        from genblaze_s3.backend import S3StorageBackend

        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client
        backend = S3StorageBackend(
            bucket="b", endpoint_url="https://s3.us-west-004.backblazeb2.com"
        )
        backend._region_verified = True
        return backend, mock_client

    def test_applies_abort_multipart_rule(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        backend.ensure_lifecycle_defaults()
        mock_client.put_bucket_lifecycle_configuration.assert_called_once()
        rules = mock_client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        # Must include the orphan-cleanup rule.
        abort_rules = [r for r in rules if "AbortIncompleteMultipartUpload" in r]
        assert len(abort_rules) == 1
        assert abort_rules[0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 7

    def test_applies_noncurrent_expire_rule(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        backend.ensure_lifecycle_defaults()
        rules = mock_client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        nc_rules = [r for r in rules if "NoncurrentVersionExpiration" in r]
        assert len(nc_rules) == 1
        assert nc_rules[0]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30

    def test_noncurrent_expire_can_be_disabled(self, mock_boto3):
        """Pass None to keep all manifest versions forever."""
        backend, mock_client = self._make_backend(mock_boto3)
        backend.ensure_lifecycle_defaults(noncurrent_version_expire_days=None)
        rules = mock_client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        assert all("NoncurrentVersionExpiration" not in r for r in rules)

    def test_lifecycle_failure_is_non_fatal(self, mock_boto3):
        """Read-only keys / IaC-managed buckets shouldn't block uploads."""
        backend, mock_client = self._make_backend(mock_boto3)
        mock_client.put_bucket_lifecycle_configuration.side_effect = RuntimeError("access denied")
        backend.ensure_lifecycle_defaults()  # must not raise


class TestForBackblaze:
    """S3StorageBackend.for_backblaze() — Backblaze B2 preset factory."""

    def test_derives_b2_endpoint_from_region(self, mock_boto3, monkeypatch):
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "env-key")
        monkeypatch.setenv("B2_APP_KEY", "env-secret")
        mock_boto3.client.return_value = MagicMock()

        S3StorageBackend.for_backblaze("my-bucket", region="us-west-004", auto_lifecycle=False)

        kwargs = mock_boto3.client.call_args.kwargs
        assert kwargs["endpoint_url"] == "https://s3.us-west-004.backblazeb2.com"
        assert kwargs["region_name"] == "us-west-004"
        assert kwargs["aws_access_key_id"] == "env-key"
        assert kwargs["aws_secret_access_key"] == "env-secret"  # noqa: S105 — test fixture

    def test_explicit_credentials_override_env(self, mock_boto3, monkeypatch):
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "env-key")
        monkeypatch.setenv("B2_APP_KEY", "env-secret")
        mock_boto3.client.return_value = MagicMock()

        S3StorageBackend.for_backblaze(
            "my-bucket",
            key_id="explicit-key",
            app_key="explicit-secret",
            auto_lifecycle=False,
        )

        kwargs = mock_boto3.client.call_args.kwargs
        assert kwargs["aws_access_key_id"] == "explicit-key"
        assert kwargs["aws_secret_access_key"] == "explicit-secret"  # noqa: S105 — test fixture

    def test_public_url_base_passthrough(self, mock_boto3, monkeypatch):
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "k")
        monkeypatch.setenv("B2_APP_KEY", "s")
        mock_boto3.client.return_value = MagicMock()

        backend = S3StorageBackend.for_backblaze(
            "my-bucket",
            public_url_base="https://f004.backblazeb2.com/file/my-bucket",
            auto_lifecycle=False,
        )
        url = backend.get_url("assets/img.png")
        assert url == "https://f004.backblazeb2.com/file/my-bucket/assets/img.png"

    def test_missing_credentials_raises_clear_error(self, mock_boto3, monkeypatch):
        """No B2_KEY_ID/B2_APP_KEY and no explicit args → fail fast with guidance.

        Without this guard, boto3 falls through to its default credential
        chain (IMDS, profiles) and fails mid-upload with an opaque
        NoCredentialsError — a classic support-ticket generator.
        """
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.delenv("B2_KEY_ID", raising=False)
        monkeypatch.delenv("B2_APP_KEY", raising=False)

        with pytest.raises(ValueError, match="B2_KEY_ID"):
            S3StorageBackend.for_backblaze("my-bucket")

    def test_auto_lifecycle_applies_defaults(self, mock_boto3, monkeypatch):
        """auto_lifecycle=True (default) calls put_bucket_lifecycle_configuration."""
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "k")
        monkeypatch.setenv("B2_APP_KEY", "s")
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        S3StorageBackend.for_backblaze("my-bucket")

        mock_client.put_bucket_lifecycle_configuration.assert_called_once()

    def test_auto_lifecycle_opt_out(self, mock_boto3, monkeypatch):
        """Users managing lifecycle in Terraform/IaC can disable the helper."""
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "k")
        monkeypatch.setenv("B2_APP_KEY", "s")
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        S3StorageBackend.for_backblaze("my-bucket", auto_lifecycle=False)

        mock_client.put_bucket_lifecycle_configuration.assert_not_called()

    def test_boto_config_carries_b2_essentials(self, mock_boto3, monkeypatch):
        """BotoConfig must carry user_agent_extra (B2 attribution), adaptive retries,
        explicit timeouts, a generous connection pool, and most importantly
        request_checksum_calculation='when_required' — which prevents
        boto3 >= 1.36 from sending x-amz-sdk-checksum-algorithm headers that
        older B2 / other S3-compat endpoints reject."""
        from genblaze_core._version import __version__
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "k")
        monkeypatch.setenv("B2_APP_KEY", "s")
        mock_boto3.client.return_value = MagicMock()

        S3StorageBackend.for_backblaze("my-bucket", auto_lifecycle=False)

        mock_config = sys.modules["botocore.config"].Config
        # Config may be called twice if region auto-detect reconfigures;
        # inspect the first call which is the one that built the client used here.
        config_kwargs = mock_config.call_args_list[0].kwargs
        assert config_kwargs["user_agent_extra"] == f"b2ai-genblaze/{__version__}"
        assert config_kwargs["retries"] == {"max_attempts": 3, "mode": "adaptive"}
        assert config_kwargs["connect_timeout"] == 30
        assert config_kwargs["read_timeout"] == 300
        assert config_kwargs["max_pool_connections"] == 20
        assert config_kwargs["request_checksum_calculation"] == "when_required"
        assert config_kwargs["response_checksum_validation"] == "when_required"
