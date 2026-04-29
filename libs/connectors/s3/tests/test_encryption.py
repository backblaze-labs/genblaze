"""Unit tests for ``genblaze_s3.encryption.Encryption``.

Phase 1A ships only the value object. Tests pin the per-mode boto3
serialization shape (Phase 1D wires it into the backend).
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from genblaze_s3 import Encryption, EncryptionMode

# 32-byte AES-256 key fixture — deterministic for snapshot tests.
_KEY = bytes(range(32))
_KEY_MD5 = base64.b64encode(hashlib.md5(_KEY).digest()).decode("ascii")  # noqa: S324


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def test_sse_s3_constructor() -> None:
    enc = Encryption.sse_s3()
    assert enc.mode is EncryptionMode.SSE_S3
    assert enc.kms_key_id is None
    assert enc.customer_key is None


def test_sse_kms_constructor() -> None:
    enc = Encryption.sse_kms("alias/my-app")
    assert enc.mode is EncryptionMode.SSE_KMS
    assert enc.kms_key_id == "alias/my-app"


def test_sse_kms_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="kms_key_id"):
        Encryption.sse_kms("")


def test_sse_c_constructor_computes_md5() -> None:
    enc = Encryption.sse_c(_KEY)
    assert enc.mode is EncryptionMode.SSE_C
    assert enc.customer_key == _KEY
    assert enc.customer_key_md5_b64 == _KEY_MD5


def test_sse_c_accepts_explicit_md5() -> None:
    enc = Encryption.sse_c(_KEY, key_md5_b64=_KEY_MD5)
    assert enc.customer_key_md5_b64 == _KEY_MD5


def test_sse_c_rejects_short_key() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        Encryption.sse_c(b"too-short")


def test_sse_c_rejects_long_key() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        Encryption.sse_c(b"x" * 64)


def test_sse_c_rejects_non_bytes() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        Encryption.sse_c("not-bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Direct construction guards
# ---------------------------------------------------------------------------


def test_direct_sse_kms_without_key_id_raises() -> None:
    with pytest.raises(ValueError, match="SSE_KMS"):
        Encryption(mode=EncryptionMode.SSE_KMS)


def test_direct_sse_c_without_customer_key_raises() -> None:
    with pytest.raises(ValueError, match="SSE_C"):
        Encryption(mode=EncryptionMode.SSE_C)


# ---------------------------------------------------------------------------
# Boto3 wire-shape serialization
# ---------------------------------------------------------------------------


def test_sse_s3_put_extra_args() -> None:
    assert Encryption.sse_s3().to_put_extra_args() == {"ServerSideEncryption": "AES256"}


def test_sse_kms_put_extra_args() -> None:
    args = Encryption.sse_kms("arn:aws:kms:us-west-2:111:key/abc").to_put_extra_args()
    assert args == {
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": "arn:aws:kms:us-west-2:111:key/abc",
    }


def test_sse_c_put_extra_args() -> None:
    args = Encryption.sse_c(_KEY).to_put_extra_args()
    assert args == {
        "SSECustomerAlgorithm": "AES256",
        "SSECustomerKey": _KEY,
        "SSECustomerKeyMD5": _KEY_MD5,
    }


def test_sse_s3_get_args_empty() -> None:
    """Server-managed keys don't need anything on the read side."""
    assert Encryption.sse_s3().to_get_extra_args() == {}
    assert Encryption.sse_kms("k").to_get_extra_args() == {}


def test_sse_c_get_args_match_put() -> None:
    """SSE-C reads must carry the same customer key (else 400)."""
    enc = Encryption.sse_c(_KEY)
    assert enc.to_get_extra_args() == {
        "SSECustomerAlgorithm": "AES256",
        "SSECustomerKey": _KEY,
        "SSECustomerKeyMD5": _KEY_MD5,
    }
    assert enc.to_head_extra_args() == enc.to_get_extra_args()


def test_sse_c_copy_carries_both_source_and_dest() -> None:
    """Copy of an encrypted object needs source-key for read AND dest-key for write."""
    args = Encryption.sse_c(_KEY).to_copy_extra_args()
    assert args == {
        "CopySourceSSECustomerAlgorithm": "AES256",
        "CopySourceSSECustomerKey": _KEY,
        "CopySourceSSECustomerKeyMD5": _KEY_MD5,
        "SSECustomerAlgorithm": "AES256",
        "SSECustomerKey": _KEY,
        "SSECustomerKeyMD5": _KEY_MD5,
    }


def test_sse_kms_copy_uses_put_shape() -> None:
    args = Encryption.sse_kms("alias/x").to_copy_extra_args()
    assert args == {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "alias/x"}


# ---------------------------------------------------------------------------
# Redaction — customer key never appears in repr / str
# ---------------------------------------------------------------------------


def test_sse_c_repr_redacts_customer_key() -> None:
    enc = Encryption.sse_c(_KEY)
    text = repr(enc)
    assert "redacted" in text
    # The raw key bytes must not show up (would be 32 byte values)
    assert _KEY.hex() not in text
    assert str(_KEY) not in text


def test_sse_c_str_redacts_customer_key() -> None:
    """``__str__`` is what f-strings/loggers call by default — must redact too."""
    enc = Encryption.sse_c(_KEY)
    text = str(enc)
    assert "redacted" in text
    assert _KEY.hex() not in text


def test_sse_kms_repr_shows_key_id() -> None:
    """KMS key ids are not secret — they're identifiers, not material."""
    enc = Encryption.sse_kms("alias/visible")
    assert "alias/visible" in repr(enc)


def test_sse_s3_repr_shows_mode_only() -> None:
    assert "sse-s3" in repr(Encryption.sse_s3())


# ---------------------------------------------------------------------------
# Hashable / frozen
# ---------------------------------------------------------------------------


def test_frozen_blocks_mutation() -> None:
    import dataclasses

    enc = Encryption.sse_kms("k")
    with pytest.raises(dataclasses.FrozenInstanceError):
        enc.kms_key_id = "other"  # type: ignore[misc]


def test_equal_instances_hash_alike() -> None:
    a = Encryption.sse_kms("alias/x")
    b = Encryption.sse_kms("alias/x")
    assert hash(a) == hash(b)
    assert a == b
