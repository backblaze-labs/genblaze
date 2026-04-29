"""Unit tests for ``genblaze_core.storage.errors``.

Covers:

* ``StorageError`` field expansion (backwards-compat with positional-only callers).
* ``classify_botocore_error`` shape: every well-known boto code → typed
  ``StorageErrorCode`` + ``request_id`` / ``status_code`` round-trip.
* ``RETRYABLE_STORAGE_CODES`` membership.

Botocore is imported lazily inside the classifier; we synthesize a
``ClientError`` directly to avoid pulling boto3 into the core test deps.
"""

from __future__ import annotations

import pytest
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.errors import (
    RETRYABLE_STORAGE_CODES,
    StorageErrorCode,
    classify_botocore_error,
)

# Botocore is a connector-side dep, not a core dep. Skip the classifier
# round-trip tests when it isn't importable in this environment.
boto_client_error = pytest.importorskip("botocore.exceptions").ClientError
botocore_exceptions = pytest.importorskip("botocore.exceptions")


# ---------------------------------------------------------------------------
# StorageError shape
# ---------------------------------------------------------------------------


def test_storage_error_positional_only_still_works() -> None:
    """11 connectors raise ``StorageError(f"...")`` — backward-compat is load-bearing."""
    err = StorageError("S3 put failed for foo: boom")
    assert str(err) == "S3 put failed for foo: boom"
    assert err.error_code is None
    assert err.request_id is None
    assert err.status_code is None
    assert err.retry_after is None
    assert err.is_retriable is False
    assert err.operation is None


def test_storage_error_full_kwargs() -> None:
    err = StorageError(
        "boom",
        error_code=StorageErrorCode.RATE_LIMIT,
        request_id="abc-123",
        status_code=429,
        retry_after=2.5,
        is_retriable=True,
        operation="put",
    )
    assert err.error_code is StorageErrorCode.RATE_LIMIT
    assert err.request_id == "abc-123"
    assert err.status_code == 429
    assert err.retry_after == 2.5
    assert err.is_retriable is True
    assert err.operation == "put"


def test_retryable_codes_set() -> None:
    """Membership is the contract callers rely on — pin it explicitly."""
    assert StorageErrorCode.RATE_LIMIT in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.SERVER_ERROR in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.NETWORK in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.TIMEOUT in RETRYABLE_STORAGE_CODES
    # Definitely-not-retriable
    assert StorageErrorCode.NOT_FOUND not in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.AUTH_FAILURE not in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.ACCESS_DENIED not in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.OBJECT_LOCKED not in RETRYABLE_STORAGE_CODES
    assert StorageErrorCode.INVALID_INPUT not in RETRYABLE_STORAGE_CODES


# ---------------------------------------------------------------------------
# classify_botocore_error
# ---------------------------------------------------------------------------


def _make_client_error(
    code: str,
    *,
    status: int | None = None,
    message: str = "synthetic",
    request_id: str | None = "req-123",
    headers: dict[str, str] | None = None,
):
    """Synthesize a botocore.ClientError with the documented response shape."""
    metadata: dict[str, object] = {}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    if request_id is not None:
        metadata["RequestId"] = request_id
    if headers is not None:
        metadata["HTTPHeaders"] = headers
    response = {
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": metadata,
    }
    return boto_client_error(response, "MockOperation")


def test_classify_404_not_found() -> None:
    err = classify_botocore_error(
        _make_client_error("404", status=404), operation="get", key="foo"
    )
    assert err.error_code is StorageErrorCode.NOT_FOUND
    assert err.status_code == 404
    assert err.request_id == "req-123"
    assert err.is_retriable is False
    assert err.operation == "get"
    assert "for 'foo'" in str(err)


def test_classify_no_such_key_alias() -> None:
    err = classify_botocore_error(_make_client_error("NoSuchKey", status=404), operation="get")
    assert err.error_code is StorageErrorCode.NOT_FOUND


def test_classify_403_access_denied() -> None:
    err = classify_botocore_error(_make_client_error("AccessDenied", status=403), operation="head")
    assert err.error_code is StorageErrorCode.ACCESS_DENIED
    assert err.is_retriable is False


def test_classify_signature_does_not_match_is_auth_failure() -> None:
    err = classify_botocore_error(
        _make_client_error("SignatureDoesNotMatch", status=403), operation="put"
    )
    assert err.error_code is StorageErrorCode.AUTH_FAILURE


def test_classify_slowdown_is_rate_limit_and_retriable() -> None:
    err = classify_botocore_error(
        _make_client_error(
            "SlowDown",
            status=503,
            headers={"retry-after": "5"},
        ),
        operation="put",
    )
    assert err.error_code is StorageErrorCode.RATE_LIMIT
    assert err.is_retriable is True
    assert err.retry_after == 5.0


def test_classify_5xx_is_server_error_and_retriable() -> None:
    # No explicit code → fall through to status-based classification.
    err = classify_botocore_error(_make_client_error("InternalError", status=500), operation="put")
    assert err.error_code is StorageErrorCode.SERVER_ERROR
    assert err.is_retriable is True


def test_classify_region_redirect() -> None:
    err = classify_botocore_error(
        _make_client_error("PermanentRedirect", status=301), operation="head"
    )
    assert err.error_code is StorageErrorCode.REGION_REDIRECT
    assert err.is_retriable is False


def test_classify_invalid_input_branches() -> None:
    err = classify_botocore_error(
        _make_client_error("InvalidArgument", status=400), operation="put"
    )
    assert err.error_code is StorageErrorCode.INVALID_INPUT


def test_classify_object_lock_conflict() -> None:
    err = classify_botocore_error(
        _make_client_error("InvalidObjectLockConfiguration", status=400), operation="put"
    )
    assert err.error_code is StorageErrorCode.OBJECT_LOCKED


def test_classify_unknown_code_falls_through_to_status() -> None:
    """Unmapped Error.Code uses HTTPStatusCode as a fallback signal."""
    err = classify_botocore_error(
        _make_client_error("WeirdNewCodeFromAWS", status=502), operation="get"
    )
    assert err.error_code is StorageErrorCode.SERVER_ERROR  # 5xx fallback


def test_classify_unknown_code_no_status_is_unknown() -> None:
    err = classify_botocore_error(_make_client_error("Mystery", status=None), operation="copy")
    assert err.error_code is StorageErrorCode.UNKNOWN
    assert err.is_retriable is False


def test_classify_connect_timeout_is_timeout_and_retriable() -> None:
    exc = botocore_exceptions.ConnectTimeoutError(endpoint_url="https://example.com")
    err = classify_botocore_error(exc, operation="put")
    assert err.error_code is StorageErrorCode.TIMEOUT
    assert err.is_retriable is True


def test_classify_connection_error_is_network_and_retriable() -> None:
    exc = botocore_exceptions.ConnectionError(error="kaboom")
    err = classify_botocore_error(exc, operation="put")
    assert err.error_code is StorageErrorCode.NETWORK
    assert err.is_retriable is True


def test_classify_non_boto_exception_is_unknown() -> None:
    err = classify_botocore_error(RuntimeError("stray exception"), operation="put")
    assert err.error_code is StorageErrorCode.UNKNOWN
    assert err.is_retriable is False
