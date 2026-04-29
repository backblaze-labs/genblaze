"""URL-policy primitive for object-storage backends.

Resolves the silent-precedence trap in :meth:`S3StorageBackend.get_url`:
when ``public_url_base`` is set, ``expires_in=`` is ignored without
raising — paid-feed scenarios that intentionally pass an expiry get back
a never-expiring public URL. Real users got bitten by this on shared
buckets.

Phase 1A introduces the value object; Phase 1D wires it into
``S3StorageBackend.get_url`` and removes the silent precedence.
"""

from __future__ import annotations

from enum import StrEnum

from genblaze_core.exceptions import GenblazeError


class URLPolicy(StrEnum):
    """Selects which URL flavor :meth:`get_url` returns.

    Members:
        AUTO: Default. Returns ``public_url_base`` URL when configured,
            presigned URL otherwise. Matches today's behavior — except a
            non-default ``expires_in`` while ``public_url_base`` is set
            now raises :class:`URLPolicyError` instead of silently being
            ignored.
        PUBLIC: Force a public URL via ``public_url_base``. Raises
            :class:`URLPolicyError` if ``public_url_base`` is not set, or
            if ``expires_in`` is also passed (conflict — public URLs
            don't expire).
        PRESIGNED: Force a presigned URL via SigV4. Always honors
            ``expires_in``. Useful when a bucket has ``public_url_base``
            configured but the caller wants a credential-bearing URL for
            a paid-feed / time-limited fetch.
    """

    AUTO = "auto"
    PUBLIC = "public"
    PRESIGNED = "presigned"


class URLPolicyError(GenblazeError):
    """Raised on a URL-policy conflict at construction or call time.

    Examples:

    * ``policy=URLPolicy.PUBLIC`` with ``expires_in`` set (conflict —
      public URLs don't carry an expiry).
    * ``policy=URLPolicy.PUBLIC`` on a backend with no
      ``public_url_base`` configured.

    Subclass of :class:`GenblazeError` for catch-all compatibility.
    """


__all__ = ["URLPolicy", "URLPolicyError"]
