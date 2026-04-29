"""``PresignedURL`` value object — credential-bearing URL that redacts in logs.

Resolves bug #1 from the storage-backend-hardening tranche: today
:meth:`S3StorageBackend.put` returns a presigned URL string. The
``X-Amz-Credential`` query parameter embeds the access-key-id, and
``X-Amz-Signature`` is a SigV4 HMAC. Persisting either to a log,
manifest, or DB row leaks transient credentials and lets anyone replay
the fetch within the expiry window.

The fix is two-pronged:

1. :meth:`put` returns the storage key (a plain :class:`str`) instead
   of a presigned URL — opt-in to a presigned URL via the dedicated
   :meth:`presigned_get` / :meth:`presigned_put` / :meth:`presigned_post`
   methods. Phase 1B owns this signature change with a deprecation shim.

2. Those dedicated methods return a :class:`PresignedURL` whose
   ``__repr__`` AND ``__str__`` redact the signature. Callers that
   actually need the unredacted URL access it explicitly via the
   :attr:`PresignedURL.url` property — every leak site becomes a
   conscious decision instead of a silent string interpolation.

The redaction is applied to the credential / signature / token query
parameters, not to the bucket / key / region — those are already
non-secret and useful in error messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query-param names that must be redacted — these are the SigV4
# credential/signature carriers. ``X-Amz-Security-Token`` is included
# because session-token credentials embed the STS token here. Names are
# matched case-insensitively for resilience to non-canonical encoders.
_REDACTED_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "signature",  # legacy SigV2
        "awsaccesskeyid",  # legacy SigV2
    }
)

_REDACTED_VALUE = "redacted"


def _redact_url(url: str) -> str:
    """Return ``url`` with credential-bearing query params replaced.

    Preserves the rest of the URL (scheme, host, path, non-secret query
    params) so error messages remain useful. Idempotent — running the
    function twice produces the same string.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    redacted_pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _REDACTED_QUERY_PARAMS:
            redacted_pairs.append((key, _REDACTED_VALUE))
        else:
            redacted_pairs.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(redacted_pairs)))


@dataclass(frozen=True, slots=True)
class PresignedURL:
    """A short-lived, credential-bearing URL with redaction-safe formatting.

    Attributes:
        url: The full presigned URL — including ``X-Amz-Signature`` and
            ``X-Amz-Credential``. Use this when handing the URL to an
            HTTP client; do NOT log it directly. The dataclass field is
            ``repr=False`` so the raw URL never appears in default repr
            output.
        method: HTTP method the URL is signed for (``"GET"`` for
            :meth:`presigned_get`, ``"PUT"`` for :meth:`presigned_put`,
            ``"POST"`` for :meth:`presigned_post`).
        key: Storage key the URL fetches/uploads. Non-secret; surfaces in
            error messages.
        bucket: Bucket the URL targets. Non-secret.
        expires_in: Seconds until expiry from issue-time.

    Logging policy:

    * ``repr(presigned)`` and ``str(presigned)`` redact the signature
      and credential query params — safe to log via either ``%s`` or
      ``%r``.
    * ``presigned.url`` returns the unredacted URL — call it
      explicitly when handing to ``requests.get(...)`` or similar.
    """

    url: str = field(repr=False)
    method: Literal["GET", "PUT", "POST"]
    key: str
    bucket: str
    expires_in: int

    def __repr__(self) -> str:
        return (
            f"PresignedURL(method={self.method!r}, bucket={self.bucket!r}, "
            f"key={self.key!r}, expires_in={self.expires_in}, "
            f"url={_redact_url(self.url)!r})"
        )

    def __str__(self) -> str:
        # Same redacted form as repr — most accidental leaks happen via
        # f-string / .format / logger %s, all of which call __str__.
        # Callers that need the raw URL access ``.url`` explicitly.
        return self.__repr__()


__all__ = ["PresignedURL"]
