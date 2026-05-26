"""URL-policy primitive for object-storage backends and sinks.

Lives in ``genblaze_core`` because both the backend layer (e.g.
``S3StorageBackend.get_url``) AND the sink layer (``ObjectStorageSink``,
which selects the URL flavor written into ``asset.url``) need a shared
vocabulary. Originally introduced in ``genblaze_s3`` during the storage-
backend-hardening tranche; relocated to core in 0.3.1 so the sink (which
lives in core) could reference it without ``genblaze-core`` developing
a circular dependency on ``genblaze-s3``. The ``genblaze_s3.url_policy``
module remains as a thin back-compat re-export.

Resolves the silent-precedence trap in :meth:`S3StorageBackend.get_url`:
when ``public_url_base`` is set, ``expires_in=`` is ignored without
raising — paid-feed scenarios that intentionally pass an expiry get back
a never-expiring public URL. Real users got bitten by this on shared
buckets.
"""

from __future__ import annotations

from enum import StrEnum

from genblaze_core.exceptions import GenblazeError


class URLPolicy(StrEnum):
    """Selects which URL flavor a backend's ``get_url`` returns, or which
    flavor a sink writes into ``asset.url``.

    Members:
        AUTO: Default. On a backend's ``get_url``, returns
            ``public_url_base`` URL when configured, presigned URL
            otherwise (permissive — preserves the historic behavior of
            silently ignoring ``expires_in`` when ``public_url_base`` is
            set). On a sink, writes ``get_durable_url(key)`` into
            ``asset.url`` regardless of whether ``public_url_base`` is
            configured — emits a one-time WARN at construction when the
            backend has no ``public_url_base`` to alert the caller that
            the durable URL may not be browser-loadable.
        PUBLIC: Force a public URL via ``public_url_base``. Raises
            :class:`URLPolicyError` if ``public_url_base`` is not set, or
            (on backend ``get_url``) if ``expires_in`` is also passed
            explicitly. Use this when your code path expects a
            never-expiring URL and you want the SDK to fail loudly on
            misconfiguration.
        PRESIGNED: Force a presigned URL via SigV4. Always honors
            ``expires_in`` on backend ``get_url``. **Rejected at sink
            construction** — manifests must not carry credential-bearing
            SigV4 URLs (they decay before the manifest does, breaking
            provenance). For read-time presigned URLs on a per-asset
            basis, use ``backend.presigned_get_url(key)`` directly.
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
    * ``asset_url_policy=URLPolicy.PRESIGNED`` passed to
      ``ObjectStorageSink`` (rejected — manifests cannot carry SigV4).

    Subclass of :class:`GenblazeError` for catch-all compatibility.
    """


__all__ = ["URLPolicy", "URLPolicyError"]
