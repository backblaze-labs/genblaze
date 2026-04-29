"""Typed error classification for storage backends.

Mirrors :mod:`genblaze_core.providers.retry` for the storage subsystem:

* :class:`StorageErrorCode` — typed enum for backend failure modes.
* :data:`RETRYABLE_STORAGE_CODES` — frozenset of codes the retry helper
  may retry without changing the request.
* :func:`classify_botocore_error` — maps a ``botocore.ClientError`` to a
  populated :class:`StorageError` with ``error_code``, ``request_id``,
  ``status_code``, and ``is_retriable`` set.

The :class:`StorageError` exception itself lives in
:mod:`genblaze_core.exceptions` alongside the rest of the genblaze
exception hierarchy. This module does not redefine it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from genblaze_core.exceptions import StorageError

if TYPE_CHECKING:
    pass


class StorageErrorCode(StrEnum):
    """Typed classification for object-storage failures.

    Mirrors :class:`genblaze_core.models.enums.ProviderErrorCode` shape but
    covers transport/backend concerns rather than generation concerns.
    """

    # Object-not-found / GET on a missing key. Never retriable — a missing
    # object will not become present without external action.
    NOT_FOUND = "not_found"
    # 403 / AccessDenied. Distinguished from AUTH_FAILURE because some
    # least-privilege keys legitimately get 403 on reads of non-existent
    # keys (B2 behavior); callers may want to treat both alike.
    ACCESS_DENIED = "access_denied"
    # Bad credentials, expired token, signature mismatch. Not retriable.
    AUTH_FAILURE = "auth_failure"
    # Bucket lives in a different region; some endpoints surface 301 with an
    # ``x-amz-bucket-region`` header. Backends that auto-redirect handle
    # this internally; surfacing the code is for callers that pin region.
    REGION_REDIRECT = "region_redirect"
    # 429 / SlowDown / ThrottlingException. Honor ``Retry-After`` if set.
    RATE_LIMIT = "rate_limit"
    # 5xx — transient upstream failure. Retriable with backoff.
    SERVER_ERROR = "server_error"
    # Connection refused, DNS, TLS, etc. Retriable; usually a brief network
    # blip or DNS failover.
    NETWORK = "network"
    # Read/connect timeout. Retriable.
    TIMEOUT = "timeout"
    # Caller-supplied input is malformed (bad bucket name, key too long,
    # invalid checksum, conflicting kwargs). Never retriable.
    INVALID_INPUT = "invalid_input"
    # SSE-C / KMS key required for the operation but not provided. Calls
    # into Phase 1's ``Encryption`` value object on resolution.
    ENCRYPTION_REQUIRED = "encryption_required"
    # Object Lock retention prevents the operation. Not retriable —
    # the lock cannot be shortened.
    OBJECT_LOCKED = "object_locked"
    UNKNOWN = "unknown"


# Codes that are safe to retry without changing the request. Mirrors
# :data:`genblaze_core.models.enums.RETRYABLE_ERROR_CODES` for the storage
# subsystem.
RETRYABLE_STORAGE_CODES: frozenset[StorageErrorCode] = frozenset(
    {
        StorageErrorCode.RATE_LIMIT,
        StorageErrorCode.SERVER_ERROR,
        StorageErrorCode.NETWORK,
        StorageErrorCode.TIMEOUT,
    }
)


# Map of botocore Error.Code values → typed StorageErrorCode. Anything not
# listed falls through to an HTTP-status-based classification, then UNKNOWN.
_BOTO_ERROR_CODE_MAP: dict[str, StorageErrorCode] = {
    "404": StorageErrorCode.NOT_FOUND,
    "NoSuchKey": StorageErrorCode.NOT_FOUND,
    "NoSuchBucket": StorageErrorCode.NOT_FOUND,
    "403": StorageErrorCode.ACCESS_DENIED,
    "AccessDenied": StorageErrorCode.ACCESS_DENIED,
    "Forbidden": StorageErrorCode.ACCESS_DENIED,
    "InvalidAccessKeyId": StorageErrorCode.AUTH_FAILURE,
    "SignatureDoesNotMatch": StorageErrorCode.AUTH_FAILURE,
    "ExpiredToken": StorageErrorCode.AUTH_FAILURE,
    "TokenRefreshRequired": StorageErrorCode.AUTH_FAILURE,
    "301": StorageErrorCode.REGION_REDIRECT,
    "PermanentRedirect": StorageErrorCode.REGION_REDIRECT,
    "AuthorizationHeaderMalformed": StorageErrorCode.REGION_REDIRECT,
    "SlowDown": StorageErrorCode.RATE_LIMIT,
    "Throttling": StorageErrorCode.RATE_LIMIT,
    "ThrottlingException": StorageErrorCode.RATE_LIMIT,
    "TooManyRequests": StorageErrorCode.RATE_LIMIT,
    "RequestTimeout": StorageErrorCode.TIMEOUT,
    "RequestTimeoutException": StorageErrorCode.TIMEOUT,
    "InvalidArgument": StorageErrorCode.INVALID_INPUT,
    "InvalidBucketName": StorageErrorCode.INVALID_INPUT,
    "EntityTooLarge": StorageErrorCode.INVALID_INPUT,
    "InvalidPart": StorageErrorCode.INVALID_INPUT,
    "BadDigest": StorageErrorCode.INVALID_INPUT,
    "MalformedXML": StorageErrorCode.INVALID_INPUT,
    "InvalidEncryptionAlgorithmError": StorageErrorCode.ENCRYPTION_REQUIRED,
    "InvalidObjectLockConfiguration": StorageErrorCode.OBJECT_LOCKED,
    "ObjectLockConfigurationNotFoundError": StorageErrorCode.OBJECT_LOCKED,
}


def _status_code_to_storage_code(status: int) -> StorageErrorCode:
    """Fallback classification from raw HTTP status when no Error.Code matched."""
    if status == 404:
        return StorageErrorCode.NOT_FOUND
    if status == 403:
        return StorageErrorCode.ACCESS_DENIED
    if status == 429:
        return StorageErrorCode.RATE_LIMIT
    if 500 <= status < 600:
        return StorageErrorCode.SERVER_ERROR
    if 400 <= status < 500:
        return StorageErrorCode.INVALID_INPUT
    return StorageErrorCode.UNKNOWN


def _parse_retry_after(headers: dict[str, Any]) -> float | None:
    """Pull a ``Retry-After`` value from boto3 response headers.

    Boto3 normalizes header keys to lowercase but we look at both for
    safety. Numeric seconds is the common shape; HTTP-date is rare for S3
    APIs and is intentionally not parsed here (the retry helper clamps
    upstream).
    """
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def classify_botocore_error(
    exc: Exception,
    *,
    operation: str,
    key: str | None = None,
) -> StorageError:
    """Map a botocore ``ClientError`` (or any boto exception) to a typed
    :class:`StorageError`.

    Pulls ``request_id``, ``status_code``, and ``Retry-After`` from the
    response envelope when available. Non-``ClientError`` exceptions are
    wrapped as ``UNKNOWN``/``NETWORK`` based on the type so callers always
    get a typed shape regardless of the underlying failure mode.

    Args:
        exc: The original exception raised by the boto3/botocore stack.
        operation: The backend method that raised, e.g. ``"put"``, ``"get"``.
            Surfaces on :attr:`StorageError.operation`.
        key: Optional key the operation was targeting; included in the
            error message so logs/tracing show what failed.

    Returns:
        A populated :class:`StorageError` ready to raise.
    """
    # Lazy import — botocore lives in the genblaze-s3 connector, not in
    # core's runtime deps. ``classify_botocore_error`` is only called from
    # connector code that already has botocore loaded.
    try:
        from botocore.exceptions import (
            ClientError,
            ConnectTimeoutError,
            ReadTimeoutError,
        )
        from botocore.exceptions import (
            ConnectionError as BotoConnectionError,
        )
    except ImportError:  # pragma: no cover — only hit if invoked w/o boto installed
        # Fallback: surface the original exception under UNKNOWN. Don't
        # pretend we classified it.
        return StorageError(
            f"Storage {operation} failed (botocore not importable): {exc}",
            error_code=StorageErrorCode.UNKNOWN,
            operation=operation,
        )

    target = f" for {key!r}" if key else ""

    # Network-shaped exceptions: connection refused, DNS failures, etc.
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        return StorageError(
            f"Storage {operation}{target} timed out: {exc}",
            error_code=StorageErrorCode.TIMEOUT,
            is_retriable=True,
            operation=operation,
        )
    if isinstance(exc, BotoConnectionError):
        return StorageError(
            f"Storage {operation}{target} network error: {exc}",
            error_code=StorageErrorCode.NETWORK,
            is_retriable=True,
            operation=operation,
        )

    if not isinstance(exc, ClientError):
        # Non-boto exception slipped through. Wrap as UNKNOWN — caller
        # decides whether to retry based on context.
        return StorageError(
            f"Storage {operation}{target} failed: {exc}",
            error_code=StorageErrorCode.UNKNOWN,
            operation=operation,
        )

    response = exc.response or {}
    error_block = response.get("Error", {}) or {}
    metadata = response.get("ResponseMetadata", {}) or {}
    headers = metadata.get("HTTPHeaders", {}) or {}

    boto_code = error_block.get("Code") or ""
    status = metadata.get("HTTPStatusCode")
    request_id = metadata.get("RequestId") or headers.get("x-amz-request-id")

    code = _BOTO_ERROR_CODE_MAP.get(boto_code)
    if code is None and isinstance(status, int):
        code = _status_code_to_storage_code(status)
    if code is None:
        code = StorageErrorCode.UNKNOWN

    msg = error_block.get("Message") or str(exc)
    return StorageError(
        f"Storage {operation}{target} failed: {msg}",
        error_code=code,
        request_id=request_id,
        status_code=status if isinstance(status, int) else None,
        retry_after=_parse_retry_after(headers),
        is_retriable=code in RETRYABLE_STORAGE_CODES,
        operation=operation,
    )


__all__ = [
    "StorageErrorCode",
    "RETRYABLE_STORAGE_CODES",
    "classify_botocore_error",
    # Re-export StorageError for convenience — callers can import everything
    # storage-error-related from one module.
    "StorageError",
]
