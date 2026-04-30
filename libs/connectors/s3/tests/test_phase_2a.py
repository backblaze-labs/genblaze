"""Phase 2A regression tests — head / list / get_range / stream on S3.

Each new method is exercised with a mocked boto3 client. Tests cover:

* **head** — happy path returns ObjectMetadata; 404/403 return None
  (parity with exists tolerance); other errors raise StorageError;
  encryption= passes through.
* **list** — pagination via continuation_token; truncated vs.
  exhausted; empty bucket; max_keys validation.
* **get_range** — Range header shape; offset/length validation;
  zero-length short-circuit.
* **stream** — chunked yields; empty body; chunk_size validation;
  body.close() called on exhaustion.
* **Async pairs** — ahead and aget_range delegate to sync via
  asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.types import ListPage, ObjectMetadata
from genblaze_s3 import Encryption

from tests.conftest import _FakeClientError

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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
    backend._region_verified = True
    return backend, mock_client


# ---------------------------------------------------------------------------
# head
# ---------------------------------------------------------------------------


class TestHead:
    def test_returns_object_metadata_on_hit(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.return_value = {
            "ContentLength": 1024,
            "LastModified": _NOW,
            "ETag": '"abc123"',
            "ContentType": "image/png",
            "StorageClass": "STANDARD",
            "Metadata": {"x-app": "demo"},
        }
        meta = backend.head("k")
        assert isinstance(meta, ObjectMetadata)
        assert meta.key == "k"
        assert meta.size == 1024
        assert meta.last_modified == _NOW
        assert meta.etag == '"abc123"'
        assert meta.content_type == "image/png"
        assert meta.storage_class == "STANDARD"
        assert meta.metadata == {"x-app": "demo"}

    def test_returns_none_on_404(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.side_effect = _FakeClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        assert backend.head("missing") is None

    def test_returns_none_on_403_for_scoped_keys(self, mock_boto3):
        """B2/AWS scoped application keys (ReadFiles without ListFiles) get
        403 on HEAD for non-existent keys. Treat as missing — same
        tolerance as `exists`."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.side_effect = _FakeClientError(
            {"Error": {"Code": "403"}}, "HeadObject"
        )
        assert backend.head("missing") is None

    def test_other_errors_raise_storage_error(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.side_effect = _FakeClientError(
            {"Error": {"Code": "InternalError"}}, "HeadObject"
        )
        with pytest.raises(StorageError, match="head failed"):
            backend.head("k")

    def test_encryption_kwarg_passes_sse_c_envelope(self, mock_boto3):
        """SSE-C HEADs require the customer-key envelope just like GETs."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.return_value = {
            "ContentLength": 0,
            "LastModified": _NOW,
            "ETag": "",
        }
        key = bytes(range(32))
        backend.head("k", encryption=Encryption.sse_c(key))
        kwargs = mock_client.head_object.call_args.kwargs
        assert kwargs["SSECustomerAlgorithm"] == "AES256"
        assert kwargs["SSECustomerKey"] == key

    def test_metadata_default_empty_when_missing(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.return_value = {
            "ContentLength": 0,
            "LastModified": _NOW,
            "ETag": "",
            # No Metadata key at all.
        }
        meta = backend.head("k")
        assert meta is not None
        assert meta.metadata == {}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_bucket_returns_empty_page(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = {}
        page = backend.list()
        assert isinstance(page, ListPage)
        assert page.entries == ()
        assert page.next_token is None

    def test_single_page_with_entries(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": "run-1/manifest.json",
                    "Size": 512,
                    "LastModified": _NOW,
                    "ETag": '"abc"',
                    "StorageClass": "STANDARD",
                },
                {
                    "Key": "run-2/manifest.json",
                    "Size": 1024,
                    "LastModified": _NOW,
                    "ETag": '"def"',
                },
            ],
            "IsTruncated": False,
        }
        page = backend.list(prefix="run-")
        assert len(page.entries) == 2
        assert page.entries[0].key == "run-1/manifest.json"
        assert page.entries[0].size == 512
        assert page.entries[0].storage_class == "STANDARD"
        assert page.entries[1].storage_class is None  # missing in response
        assert page.next_token is None

    def test_truncated_page_returns_next_token(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "k1", "Size": 1, "LastModified": _NOW, "ETag": '"x"'},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "cursor-2",
        }
        page = backend.list()
        assert page.next_token == "cursor-2"  # noqa: S105 — pagination cursor, not a password

    def test_pagination_passes_continuation_token(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = {
            "Contents": [],
            "IsTruncated": False,
        }
        backend.list(prefix="run-", max_keys=500, continuation_token="cursor-1")
        kwargs = mock_client.list_objects_v2.call_args.kwargs
        assert kwargs["Bucket"] == "my-bucket"
        assert kwargs["Prefix"] == "run-"
        assert kwargs["MaxKeys"] == 500
        assert kwargs["ContinuationToken"] == "cursor-1"

    def test_max_keys_validation(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(ValueError, match="max_keys must be"):
            backend.list(max_keys=0)

    def test_error_wraps_as_storage_error(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.side_effect = _FakeClientError(
            {"Error": {"Code": "AccessDenied"}}, "ListObjectsV2"
        )
        with pytest.raises(StorageError, match="list failed"):
            backend.list(prefix="x")


# ---------------------------------------------------------------------------
# get_range
# ---------------------------------------------------------------------------


class TestGetRange:
    def test_basic_range(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b"0123456789"
        mock_client.get_object.return_value = {"Body": body}
        out = backend.get_range("k", offset=0, length=10)
        assert out == b"0123456789"
        kwargs = mock_client.get_object.call_args.kwargs
        # HTTP Range is inclusive on both ends — bytes=0-9 is 10 bytes.
        assert kwargs["Range"] == "bytes=0-9"

    def test_range_with_offset(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b"5678"
        mock_client.get_object.return_value = {"Body": body}
        backend.get_range("k", offset=5, length=4)
        kwargs = mock_client.get_object.call_args.kwargs
        assert kwargs["Range"] == "bytes=5-8"

    def test_zero_length_short_circuits(self, mock_boto3):
        """Zero-length must NOT contact the backend — useful for callers
        that build ranges from arithmetic that may collapse."""
        backend, mock_client = _make_backend(mock_boto3)
        out = backend.get_range("k", offset=10, length=0)
        assert out == b""
        mock_client.get_object.assert_not_called()

    def test_negative_offset_rejected(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(ValueError, match="offset must be ≥ 0"):
            backend.get_range("k", offset=-1, length=10)

    def test_negative_length_rejected(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(ValueError, match="length must be ≥ 0"):
            backend.get_range("k", offset=0, length=-1)

    def test_encryption_passes_through(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b""
        mock_client.get_object.return_value = {"Body": body}
        key = bytes(range(32))
        backend.get_range("k", offset=0, length=1, encryption=Encryption.sse_c(key))
        kwargs = mock_client.get_object.call_args.kwargs
        assert kwargs["SSECustomerKey"] == key
        assert kwargs["Range"] == "bytes=0-0"


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------


class TestStream:
    def test_yields_chunks_until_empty(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        # Simulate four 1-byte reads then EOF.
        body.read.side_effect = [b"a", b"b", b"c", b""]
        mock_client.get_object.return_value = {"Body": body}
        chunks = list(backend.stream("k", chunk_size=1))
        assert chunks == [b"a", b"b", b"c"]
        # Iterator must close the body on exhaustion.
        body.close.assert_called_once()

    def test_default_chunk_size_passes_through(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b""]
        mock_client.get_object.return_value = {"Body": body}
        list(backend.stream("k"))
        # First .read() should have been called with the default chunk_size
        # (8 MiB). The iterator may call .read() again before EOF; check
        # the first call.
        first_call = body.read.call_args_list[0]
        assert first_call.args[0] == 8 * 1024 * 1024

    def test_custom_chunk_size_validates(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        # Generator validation must fire when iteration starts. The
        # generator is created lazily, so we need to trigger __next__.
        with pytest.raises(ValueError, match="chunk_size must be"):
            next(iter(backend.stream("k", chunk_size=0)))

    def test_empty_body_yields_nothing(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b""]
        mock_client.get_object.return_value = {"Body": body}
        assert list(backend.stream("k")) == []
        body.close.assert_called_once()

    def test_get_object_failure_wraps_as_storage_error(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.get_object.side_effect = _FakeClientError(
            {"Error": {"Code": "AccessDenied"}}, "GetObject"
        )
        with pytest.raises(StorageError, match="stream failed"):
            list(backend.stream("k"))

    def test_encryption_passes_to_get_object(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b""]
        mock_client.get_object.return_value = {"Body": body}
        key = bytes(range(32))
        list(backend.stream("k", encryption=Encryption.sse_c(key)))
        kwargs = mock_client.get_object.call_args.kwargs
        assert kwargs["SSECustomerKey"] == key

    def test_partial_consumption_closes_body(self, mock_boto3):
        """Caller breaks out of iteration mid-stream — body.close() must
        still fire so the connection returns to the pool."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b"a", b"b", b"c", b""]
        mock_client.get_object.return_value = {"Body": body}

        gen = backend.stream("k", chunk_size=1)
        first = next(gen)
        assert first == b"a"
        gen.close()
        body.close.assert_called_once()


# ---------------------------------------------------------------------------
# Async pairs
# ---------------------------------------------------------------------------


class TestAsyncPairs:
    def test_ahead_delegates(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.head_object.return_value = {
            "ContentLength": 1,
            "LastModified": _NOW,
            "ETag": "",
        }
        meta = asyncio.run(backend.ahead("k"))
        assert meta is not None
        assert meta.size == 1

    def test_aget_range_delegates(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b"hi"
        mock_client.get_object.return_value = {"Body": body}
        out = asyncio.run(backend.aget_range("k", offset=0, length=2))
        assert out == b"hi"
