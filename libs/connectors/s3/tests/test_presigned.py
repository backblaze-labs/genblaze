"""Unit tests for ``genblaze_s3.presigned.PresignedURL``.

The redaction guarantee is the headline contract: ``__repr__`` and
``__str__`` both strip the SigV4 ``X-Amz-Signature`` / ``X-Amz-Credential``
query params; only the ``.url`` accessor returns the unredacted value.
"""

from __future__ import annotations

from genblaze_s3 import PresignedURL

# Realistic SigV4 URL shape — the credential, signature, and security
# token are the only three fields a logger leak would expose.
_SIGNED = (
    "https://example.s3.us-west-2.amazonaws.com/path/key.jpg"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAEXAMPLE%2F20260429%2Fus-west-2%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260429T000000Z"
    "&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=deadbeefcafef00d0123456789abcdef"
    "&X-Amz-Security-Token=session-token-blob"
)


def _make() -> PresignedURL:
    return PresignedURL(
        url=_SIGNED,
        method="GET",
        key="path/key.jpg",
        bucket="example",
        expires_in=3600,
    )


def test_url_property_is_unredacted() -> None:
    """The ``.url`` accessor is the explicit "give me the full URL" path."""
    p = _make()
    assert p.url == _SIGNED
    assert "deadbeefcafef00d" in p.url


def test_repr_redacts_signature() -> None:
    p = _make()
    text = repr(p)
    assert "deadbeefcafef00d" not in text
    assert "AKIAEXAMPLE" not in text
    assert "session-token-blob" not in text


def test_repr_keeps_non_secret_metadata() -> None:
    """Non-secret fields surface in repr — they're useful in error messages."""
    text = repr(_make())
    assert "example" in text  # bucket
    assert "path/key.jpg" in text  # key
    assert "GET" in text  # method
    assert "3600" in text  # expires_in


def test_str_also_redacts() -> None:
    """``__str__`` is what ``f"{p}"`` and ``%s`` log lines call — must redact too."""
    p = _make()
    text = str(p)
    assert "deadbeefcafef00d" not in text
    assert "AKIAEXAMPLE" not in text


def test_redaction_preserves_query_param_names() -> None:
    """Redaction replaces the *value*; the param name stays so repr is debuggable."""
    text = repr(_make())
    assert "X-Amz-Signature=" in text
    assert "X-Amz-Credential=" in text
    # And the redacted value sentinel appears
    assert "redacted" in text


def test_redaction_idempotent() -> None:
    """Calling redaction twice produces the same string — no double-redaction."""
    from genblaze_s3.presigned import _redact_url

    once = _redact_url(_SIGNED)
    twice = _redact_url(once)
    assert once == twice


def test_url_without_query_passes_through() -> None:
    """``__str__`` on a URL with no query (e.g. public URL accidentally wrapped)
    should not crash, and should not mangle the URL."""
    from genblaze_s3.presigned import _redact_url

    plain = "https://example.com/path/file.bin"
    assert _redact_url(plain) == plain


def test_legacy_sigv2_signature_redacted() -> None:
    """Older ``Signature=`` / ``AWSAccessKeyId=`` query params also redacted."""
    from genblaze_s3.presigned import _redact_url

    legacy = (
        "https://example.s3.amazonaws.com/key"
        "?AWSAccessKeyId=AKIA12345&Signature=abc%2Fdef&Expires=12345"
    )
    redacted = _redact_url(legacy)
    assert "abc%2Fdef" not in redacted
    assert "AKIA12345" not in redacted
    assert "Expires=12345" in redacted  # non-secret


def test_frozen_blocks_mutation() -> None:
    import dataclasses

    import pytest

    p = _make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.url = "other"  # type: ignore[misc]
