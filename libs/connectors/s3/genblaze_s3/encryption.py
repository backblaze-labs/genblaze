"""``Encryption`` value object — symmetric SSE config for put/get/copy/head.

Resolves bug #3 from the storage-backend-hardening tranche: SSE-C support
is currently asymmetric (works on ``put`` via ``extra_args``, but ``get``
and ``copy`` never plumbed the customer key, so encrypted objects
silently 403 on read). The value object accepts the same ``Encryption``
on every method and serializes to the right boto3 kwargs per operation.

Three modes:

* :class:`SSE_S3` — server-managed AES-256 keys (no extra config required).
* :class:`SSE_KMS` — KMS-managed keys (requires ``kms_key_id``).
* :class:`SSE_C` — customer-managed keys (requires the raw 32-byte key
  and its base64-MD5; the SDK computes the MD5 if only the key is passed).

All three are constructed via classmethods so callers don't need to know
the boto3 envelope shape:

>>> Encryption.sse_kms("arn:aws:kms:us-west-2:123:key/abc")
>>> Encryption.sse_c(b"\\x00" * 32)
>>> Encryption.sse_s3()

Phase 1A ships the value object; Phase 1D wires it into the S3 backend
``put`` / ``get`` / ``copy`` / ``head`` paths.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EncryptionMode(StrEnum):
    """SSE algorithm selection."""

    SSE_S3 = "sse-s3"
    SSE_KMS = "sse-kms"
    SSE_C = "sse-c"


# Sentinel string in repr/str output anywhere a customer key would appear.
_REDACTED = "<sse-c-key:redacted>"


@dataclass(frozen=True, slots=True)
class Encryption:
    """Symmetric encryption config for storage backend operations.

    Construct via the :meth:`sse_s3`, :meth:`sse_kms`, or :meth:`sse_c`
    classmethods rather than directly — they validate the per-mode
    constraints (e.g. ``sse_kms`` requires a key id, ``sse_c`` requires a
    32-byte raw key).

    All instances are frozen, hashable, and slot-allocated. The ``__repr__``
    and ``__str__`` of an SSE-C instance redacts the customer key — the
    raw bytes are only retrievable by direct attribute access (which is
    intentional; callers should hand them straight to the boto3 client).
    """

    mode: EncryptionMode
    kms_key_id: str | None = None
    # Raw 32-byte AES-256 key for SSE-C. Stored as bytes; redacted in repr.
    customer_key: bytes | None = field(default=None, repr=False)
    # Base64-encoded MD5 of the customer key (S3 wire-format requirement).
    # Computed from ``customer_key`` if not supplied explicitly.
    customer_key_md5_b64: str | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def sse_s3(cls) -> Encryption:
        """Server-managed AES-256 encryption — no extra config required.

        Equivalent to ``ServerSideEncryption: AES256`` on the boto3 wire.
        """
        return cls(mode=EncryptionMode.SSE_S3)

    @classmethod
    def sse_kms(cls, kms_key_id: str) -> Encryption:
        """KMS-managed encryption with the given key id (ARN, alias, or short id).

        Args:
            kms_key_id: AWS KMS key identifier. Examples:
                ``"alias/my-app"``, ``"abc-123-uuid"``,
                ``"arn:aws:kms:us-west-2:111:key/abc"``.

        Raises:
            ValueError: if ``kms_key_id`` is empty.
        """
        if not kms_key_id:
            raise ValueError("Encryption.sse_kms requires a non-empty kms_key_id")
        return cls(mode=EncryptionMode.SSE_KMS, kms_key_id=kms_key_id)

    @classmethod
    def sse_c(cls, customer_key: bytes, *, key_md5_b64: str | None = None) -> Encryption:
        """Customer-managed encryption.

        Args:
            customer_key: The raw 32-byte AES-256 key. **The caller owns this
                key** — S3 stores no copy. Lose it and the object is
                unrecoverable.
            key_md5_b64: Optional pre-computed base64-MD5 of the customer
                key. Computed automatically if omitted; pass explicitly
                when the surrounding system already has the digest.

        Raises:
            ValueError: if ``customer_key`` is not exactly 32 bytes.
        """
        if not isinstance(customer_key, (bytes, bytearray)) or len(customer_key) != 32:
            got = (
                len(customer_key)
                if isinstance(customer_key, (bytes, bytearray))
                else type(customer_key).__name__
            )
            raise ValueError(f"Encryption.sse_c requires a 32-byte AES-256 key; got {got}")
        if key_md5_b64 is None:
            key_md5_b64 = base64.b64encode(hashlib.md5(customer_key).digest()).decode("ascii")  # noqa: S324
        return cls(
            mode=EncryptionMode.SSE_C,
            customer_key=bytes(customer_key),
            customer_key_md5_b64=key_md5_b64,
        )

    def __post_init__(self) -> None:
        # Per-mode invariants. Constructors enforce these too, but
        # direct ``Encryption(mode=...)`` callers also need the guard.
        if self.mode is EncryptionMode.SSE_KMS and not self.kms_key_id:
            raise ValueError("Encryption(mode=SSE_KMS) requires kms_key_id")
        if self.mode is EncryptionMode.SSE_C and (
            self.customer_key is None or self.customer_key_md5_b64 is None
        ):
            raise ValueError("Encryption(mode=SSE_C) requires customer_key + customer_key_md5_b64")

    # ------------------------------------------------------------------
    # Boto3 wire-shape serialization
    # ------------------------------------------------------------------

    def to_put_extra_args(self) -> dict[str, Any]:
        """Boto3 ``ExtraArgs`` keys for ``put_object`` / ``upload_*``."""
        if self.mode is EncryptionMode.SSE_S3:
            return {"ServerSideEncryption": "AES256"}
        if self.mode is EncryptionMode.SSE_KMS:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.kms_key_id,
            }
        # SSE_C
        return {
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": self.customer_key,
            "SSECustomerKeyMD5": self.customer_key_md5_b64,
        }

    def to_get_extra_args(self) -> dict[str, Any]:
        """Boto3 kwargs for ``get_object`` / ``head_object`` / ``download_*``.

        SSE-S3 and SSE-KMS objects don't need anything on the read side
        (the bucket holds the key reference). SSE-C objects require the
        same customer key + MD5 the put used — without it, S3 returns
        400 InvalidEncryptionAlgorithmError.
        """
        if self.mode is EncryptionMode.SSE_C:
            return {
                "SSECustomerAlgorithm": "AES256",
                "SSECustomerKey": self.customer_key,
                "SSECustomerKeyMD5": self.customer_key_md5_b64,
            }
        return {}

    def to_head_extra_args(self) -> dict[str, Any]:
        """Boto3 kwargs for ``head_object``. Same as :meth:`to_get_extra_args`."""
        return self.to_get_extra_args()

    def to_copy_extra_args(self) -> dict[str, Any]:
        """Boto3 ``ExtraArgs`` for ``copy_object`` / ``copy``.

        For SSE-C copies, both the source and destination encryption
        params are required. The destination uses the same SSE config
        (re-encrypt at-rest with the same key); the ``CopySource*`` keys
        identify the source's customer key for the read side.
        """
        if self.mode is EncryptionMode.SSE_C:
            return {
                "CopySourceSSECustomerAlgorithm": "AES256",
                "CopySourceSSECustomerKey": self.customer_key,
                "CopySourceSSECustomerKeyMD5": self.customer_key_md5_b64,
                **self.to_put_extra_args(),
            }
        return self.to_put_extra_args()

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        if self.mode is EncryptionMode.SSE_C:
            return f"Encryption(mode={self.mode.value!r}, customer_key={_REDACTED})"
        if self.mode is EncryptionMode.SSE_KMS:
            return f"Encryption(mode={self.mode.value!r}, kms_key_id={self.kms_key_id!r})"
        return f"Encryption(mode={self.mode.value!r})"

    def __str__(self) -> str:
        return self.__repr__()


__all__ = ["Encryption", "EncryptionMode"]
