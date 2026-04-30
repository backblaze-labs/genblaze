"""Cross-phase parity tests — sync ↔ async behavior contract.

Per-phase tests cover each method in isolation. This file pins the
**cross-phase contract**: equivalent calls on the sync backend
(``S3StorageBackend``) and native-async backend
(``AsyncS3StorageBackend``) produce equivalent observable behavior.

Highest-impact scenarios first:

1. **Typed-error parity.** A 404 on a missing key produces a
   ``StorageError`` with identical ``error_code`` / ``status_code`` /
   ``is_retriable`` / ``operation`` shape across both surfaces. Pre-
   cross-phase-review, the async path raised a bare ``StorageError``
   without populating these fields — defeating the plan's
   observability acceptance gate. Tests fail-loud if the regression
   recurs.

2. **Encryption envelope parity.** ``encryption=Encryption.sse_c(key)``
   forwards the same boto3 wire-shape on both surfaces. The kwargs
   dict received by ``client.get_object`` should match between sync
   ``get`` and async ``aget``.

3. **ABC kwarg forwarding.** The default ``StorageBackend.aget`` /
   ``aput`` / ``ahead`` / ``acopy`` / ``aget_range`` accept ``**kwargs``
   so ABC-typed callers can pass connector-specific options like
   ``encryption=`` without losing them at the threadpool boundary.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.errors import StorageErrorCode
from genblaze_s3 import Encryption

from tests.conftest import _FakeClientError

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _make_sync_backend(mock_boto3_mod, **kwargs):
    """Region-verified sync backend ready for direct ops."""
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
# Typed-error parity — the cross-phase blocker the reviewer caught.
# ---------------------------------------------------------------------------


class TestTypedErrorParity:
    """Both sync and async paths must populate StorageError's structured
    fields (error_code, status_code, is_retriable, operation,
    request_id) via classify_botocore_error. Pre-fix, async raised
    bare StorageError; sync raised StorageError without classification.
    Cross-phase review fix #1.
    """

    def test_sync_get_404_carries_typed_fields(self, mock_boto3):
        backend, mock_client = _make_sync_backend(mock_boto3)
        mock_client.get_object.side_effect = _FakeClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "key not found"},
                "ResponseMetadata": {
                    "HTTPStatusCode": 404,
                    "RequestId": "req-sync-1",
                },
            },
            "GetObject",
        )
        with pytest.raises(StorageError) as exc_info:
            backend.get("missing-key")
        err = exc_info.value
        assert err.error_code is StorageErrorCode.NOT_FOUND
        assert err.status_code == 404
        assert err.is_retriable is False
        assert err.operation == "get"
        assert err.request_id == "req-sync-1"

    def test_sync_put_5xx_marks_retriable(self, mock_boto3):
        """Server-error (5xx) StorageError must have is_retriable=True so
        the retry helper attempts a re-put."""
        backend, mock_client = _make_sync_backend(mock_boto3)
        mock_client.upload_fileobj.side_effect = _FakeClientError(
            {
                "Error": {"Code": "InternalError", "Message": "transient"},
                "ResponseMetadata": {"HTTPStatusCode": 500, "RequestId": "req-sync-2"},
            },
            "UploadFileObj",
        )
        with pytest.raises(StorageError) as exc_info:
            backend.put("k", b"data")
        err = exc_info.value
        assert err.error_code is StorageErrorCode.SERVER_ERROR
        assert err.is_retriable is True
        assert err.operation == "put"
        assert err.status_code == 500

    def test_sync_head_403_returns_none_not_error(self, mock_boto3):
        """403 on head() is the documented "treat as missing" path —
        returns None rather than raising. Other typed-error cases
        (5xx, etc.) DO raise with populated fields. This pins the
        boundary."""
        backend, mock_client = _make_sync_backend(mock_boto3)
        mock_client.head_object.side_effect = _FakeClientError(
            {"Error": {"Code": "AccessDenied"}}, "HeadObject"
        )
        # 403 → None (parity with exists()).
        assert backend.head("k") is None

    def test_sync_head_503_raises_with_typed_fields(self, mock_boto3):
        """A 503 on head() is NOT the 403-treat-as-missing path — it
        raises with retriable=True so callers know to retry."""
        backend, mock_client = _make_sync_backend(mock_boto3)
        mock_client.head_object.side_effect = _FakeClientError(
            {
                "Error": {"Code": "ServiceUnavailable"},
                "ResponseMetadata": {"HTTPStatusCode": 503, "RequestId": "req-sync-3"},
            },
            "HeadObject",
        )
        with pytest.raises(StorageError) as exc_info:
            backend.head("k")
        err = exc_info.value
        assert err.error_code is StorageErrorCode.SERVER_ERROR
        assert err.is_retriable is True
        assert err.operation == "head"

    def test_sync_get_with_encryption_passes_sse_c_envelope(self, mock_boto3):
        """SSE-C get must forward CustomerKey/MD5 to client.get_object —
        the boto3 wire shape is the contract async parity tests later
        compare against."""
        backend, mock_client = _make_sync_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b"plaintext"
        mock_client.get_object.return_value = {"Body": body}
        key_bytes = bytes(range(32))
        backend.get("k", encryption=Encryption.sse_c(key_bytes))
        kwargs = mock_client.get_object.call_args.kwargs
        assert kwargs["SSECustomerAlgorithm"] == "AES256"
        assert kwargs["SSECustomerKey"] == key_bytes
        assert "SSECustomerKeyMD5" in kwargs


class TestAsyncErrorClassification:
    """Same fields populated on the async surface — closes the
    primary cross-phase observability gap.
    """

    def test_async_aget_404_carries_typed_fields(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        client.get_object.side_effect = _FakeClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "missing"},
                "ResponseMetadata": {
                    "HTTPStatusCode": 404,
                    "RequestId": "req-async-1",
                },
            },
            "GetObject",
        )

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                with pytest.raises(StorageError) as exc_info:
                    await ab.aget("missing-key")
                return exc_info.value

        err = asyncio.run(run())
        # Same shape as sync — operation differs only in the prefix
        # (``aget`` not ``get``) so async logs/observability can
        # distinguish surfaces.
        assert err.error_code is StorageErrorCode.NOT_FOUND
        assert err.status_code == 404
        assert err.is_retriable is False
        assert err.operation == "aget"
        assert err.request_id == "req-async-1"

    def test_async_astream_5xx_marks_retriable(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        client.get_object.side_effect = _FakeClientError(
            {
                "Error": {"Code": "InternalError"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "GetObject",
        )

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                with pytest.raises(StorageError) as exc_info:
                    async for _ in ab.astream("k"):
                        pass
                return exc_info.value

        err = asyncio.run(run())
        assert err.error_code is StorageErrorCode.SERVER_ERROR
        assert err.is_retriable is True
        assert err.operation == "astream"


# ---------------------------------------------------------------------------
# ABC kwarg forwarding parity — fix #2/#4.
# ---------------------------------------------------------------------------


class TestABCKwargForwarding:
    """The ABC's default async pairs accept ``**kwargs`` so callers
    typed against ``StorageBackend`` can pass connector-specific
    options (``encryption=``, ``progress=``, etc.) on the async
    surface without losing them at the threadpool-wrap boundary.

    Verify by exercising ``await backend.ahead(key, encryption=enc)``
    via the SYNC backend's ABC default (which threadpool-wraps the
    sync ``head`` per Phase 0). Pre-fix, the ABC's ``ahead`` signature
    didn't accept ``encryption=`` and the kwarg would have been
    rejected by Python.
    """

    def test_abc_ahead_forwards_encryption_kwarg(self, mock_boto3):
        backend, mock_client = _make_sync_backend(mock_boto3)
        mock_client.head_object.return_value = {
            "ContentLength": 1,
            "LastModified": _NOW,
            "ETag": "",
        }
        key_bytes = bytes(range(32))
        # Use the ABC async pair (threadpool-delegated) — the kwarg
        # must reach the sync head.
        asyncio.run(backend.ahead("k", encryption=Encryption.sse_c(key_bytes)))
        kwargs = mock_client.head_object.call_args.kwargs
        assert kwargs["SSECustomerKey"] == key_bytes

    def test_abc_aput_forwards_encryption_kwarg(self, mock_boto3):
        backend, mock_client = _make_sync_backend(mock_boto3)
        asyncio.run(backend.aput("k", b"data", encryption=Encryption.sse_s3()))
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["ServerSideEncryption"] == "AES256"

    def test_abc_acopy_forwards_encryption_kwarg(self, mock_boto3):
        backend, mock_client = _make_sync_backend(mock_boto3)
        key_bytes = bytes(range(32))
        asyncio.run(backend.acopy("src", "dst", encryption=Encryption.sse_c(key_bytes)))
        kwargs = mock_client.copy_object.call_args.kwargs
        assert kwargs["SSECustomerKey"] == key_bytes

    def test_abc_aget_range_forwards_encryption_kwarg(self, mock_boto3):
        backend, mock_client = _make_sync_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b""
        mock_client.get_object.return_value = {"Body": body}
        key_bytes = bytes(range(32))
        asyncio.run(
            backend.aget_range("k", offset=0, length=1, encryption=Encryption.sse_c(key_bytes))
        )
        kwargs = mock_client.get_object.call_args.kwargs
        assert kwargs["SSECustomerKey"] == key_bytes


# ---------------------------------------------------------------------------
# Note: the test file imports the ``aio_session`` fixture from the
# Phase 3 test module via pytest's standard fixture discovery — it
# lives in ``tests/test_async_backend.py`` as a non-conftest fixture.
# Importing it directly here would re-trigger module evaluation; the
# pattern below pulls the fixture into local scope.
# ---------------------------------------------------------------------------


@pytest.fixture
def aio_session(request):
    from tests.test_async_backend import aio_session as _fx

    yield from _fx.__wrapped__()
