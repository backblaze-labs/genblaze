"""Sticky-vs-transient classification for ``S3StorageBackend`` preflight.

The bucket-region preflight (``HeadBucket``) caches its outcome via
double-checked locking. Pre-Phase 1D-review, every non-redirect
``ClientError`` was cached permanently — meaning a transient 503 or
``SlowDown`` at construction time would brick the backend forever.

This helper classifies a botocore exception as either:

* **Sticky** — bad creds, wrong bucket, signature mismatch. The caller
  isn't going to fix this on a retry; cache the error and stop hitting
  HeadBucket.
* **Transient** — 5xx, throttle, network blip. Don't cache; let the
  next call re-issue HeadBucket so the backend can recover.

Reuses :data:`genblaze_core.storage.errors.RETRYABLE_STORAGE_CODES` as
the source of truth — anything classified there as retriable is, by
definition, not sticky.
"""

from __future__ import annotations

from typing import Any

from genblaze_core.storage.errors import (
    RETRYABLE_STORAGE_CODES,
    StorageErrorCode,
    classify_botocore_error,
)


def is_sticky_preflight_error(exc: Any) -> bool:
    """Return True when ``exc`` represents a permanent preflight failure.

    Permanent = re-issuing the HeadBucket would fail with the same
    error. We use the typed classification from Phase 0's
    ``classify_botocore_error`` so the policy stays in one place.

    Region-redirect errors (``301``/``PermanentRedirect``) are handled
    by the caller before this function runs and never reach here.
    """
    storage_error = classify_botocore_error(exc, operation="head_bucket")
    code = storage_error.error_code
    if code is None:
        # Unclassified — be conservative and treat as sticky so we don't
        # spin on an unknown failure mode forever.
        return True
    if code in RETRYABLE_STORAGE_CODES:
        return False
    # Region-redirect is technically not retriable but is handled before
    # we get here. List explicitly for clarity if it ever leaks through.
    if code is StorageErrorCode.REGION_REDIRECT:
        return False
    return True


__all__ = ["is_sticky_preflight_error"]
