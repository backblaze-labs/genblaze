"""Tests for S3StorageBackend — uses mock boto3."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_boto3():
    """Mock boto3 module before importing S3StorageBackend."""
    mock_mod = MagicMock()
    mock_botocore = MagicMock()
    modules = {
        "boto3": mock_mod,
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
        return backend, mock_client

    def test_put(self, mock_boto3):
        backend, mock_client = self._make_backend(mock_boto3)
        mock_client.generate_presigned_url.return_value = "https://signed/key"
        backend.put("test/key.png", b"data", content_type="image/png")
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Key"] == "test/key.png"
        assert call_kwargs["ContentType"] == "image/png"

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


class TestForBackblaze:
    """S3StorageBackend.for_backblaze() — Backblaze B2 preset factory."""

    def _boto3_call_kwargs(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock()
        return mock_boto3.client.call_args.kwargs

    def test_derives_b2_endpoint_from_region(self, mock_boto3, monkeypatch):
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "env-key")
        monkeypatch.setenv("B2_APP_KEY", "env-secret")
        mock_boto3.client.return_value = MagicMock()

        S3StorageBackend.for_backblaze("my-bucket", region="us-west-004")

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
            "my-bucket", key_id="explicit-key", app_key="explicit-secret"
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
        )
        url = backend.get_url("assets/img.png")
        assert url == "https://f004.backblazeb2.com/file/my-bucket/assets/img.png"

    def test_missing_env_vars_passes_none_to_boto3(self, mock_boto3, monkeypatch):
        """No B2_KEY_ID/B2_APP_KEY set → boto3 falls back to its own credential chain."""
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.delenv("B2_KEY_ID", raising=False)
        monkeypatch.delenv("B2_APP_KEY", raising=False)
        mock_boto3.client.return_value = MagicMock()

        S3StorageBackend.for_backblaze("my-bucket")

        kwargs = mock_boto3.client.call_args.kwargs
        # When key_id/app_key are None the backend omits them entirely,
        # letting boto3's default credential resolution take over.
        assert "aws_access_key_id" not in kwargs
        assert "aws_secret_access_key" not in kwargs

    def test_boto_config_has_user_agent_and_retries(self, mock_boto3, monkeypatch):
        """BotoConfig must carry user_agent_extra (for B2 attribution) and a
        retry policy — either being silently dropped would break B2 usage
        reporting or resilience to transient 429/503s.
        """
        from genblaze_core._version import __version__
        from genblaze_s3.backend import S3StorageBackend

        monkeypatch.setenv("B2_KEY_ID", "k")
        monkeypatch.setenv("B2_APP_KEY", "s")
        mock_boto3.client.return_value = MagicMock()

        S3StorageBackend.for_backblaze("my-bucket")

        mock_config = sys.modules["botocore.config"].Config
        mock_config.assert_called_once()
        config_kwargs = mock_config.call_args.kwargs
        assert config_kwargs["user_agent_extra"] == f"b2ai-genblaze/{__version__}"
        assert config_kwargs["retries"] == {"max_attempts": 3, "mode": "adaptive"}
