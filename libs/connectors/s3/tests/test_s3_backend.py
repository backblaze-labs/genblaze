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
