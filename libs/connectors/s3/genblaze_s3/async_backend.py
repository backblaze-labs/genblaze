"""``AsyncS3StorageBackend`` — native async S3 backend via ``aioboto3``.

Phase 3 of the storage-backend hardening tranche. The historical
``StorageBackend`` async surface (``aput`` / ``aget`` / etc.)
threadpool-wraps the sync impl by default — fine for most paths but
wrong for response-body handling, where the sync iterator can't be
faithfully adapted into an ``AsyncIterator`` without buffering the
whole body. This module ships the native counterpart.

Scope (current sub-phase):

* **Native async:** ``aget`` (single-shot bytes return) and
  ``astream`` (true ``AsyncIterator[bytes]`` via aioboto3's
  ``StreamingBody.iter_chunks``).
* **Threadpool-delegated:** every other operation —
  ``aput`` / ``ahead`` / ``alist`` / ``adelete`` / ``acopy`` /
  ``adelete_many`` / ``adelete_prefix`` / ``aget_range`` /
  ``aget_url`` / ``aget_durable_url``. These dispatch to the wrapped
  :class:`S3StorageBackend` via :func:`asyncio.to_thread`. Native
  versions are tracked as a follow-up sub-phase. ``aput`` in
  particular needs aioboto3-native multipart support which is more
  involved.

Lifecycle: this class is an **async context manager**. The aioboto3
client is opened on ``__aenter__`` and torn down on ``__aexit__``;
attempting to use a method without entering the context raises
``RuntimeError`` with a clear hint.

Construction takes the same kwargs as :class:`S3StorageBackend`. To
borrow a pre-configured sync backend's settings, use
:meth:`AsyncS3StorageBackend.from_sync`::

    sync_backend = S3StorageBackend.for_backblaze("my-bucket")
    async with AsyncS3StorageBackend.from_sync(sync_backend) as ab:
        data = await ab.aget("k")

The ``aioboto3`` package is an optional dep — install via
``pip install genblaze-s3[async]``. Importing this module without
``aioboto3`` available raises a clean :class:`ImportError` with the
extras hint (lazy import — the bare ``genblaze_s3`` module remains
importable on minimal installs).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

from genblaze_core.storage.errors import classify_botocore_error
from genblaze_core.storage.types import TransferProgress

from genblaze_s3.backend import _MAX_POOL_CONNECTIONS, _USER_AGENT
from genblaze_s3.encryption import Encryption

if TYPE_CHECKING:
    from genblaze_s3.backend import S3StorageBackend

logger = logging.getLogger("genblaze.s3.async")


def _require_aioboto3() -> Any:
    """Lazy-import aioboto3 with a clear error for missing optional dep.

    Raised at construction-time rather than at module-import-time so
    ``genblaze_s3.async_backend`` can be imported even when aioboto3
    isn't available — useful for tooling that introspects module
    attributes (`inspect.getmembers`, sphinx, etc.).
    """
    try:
        import aioboto3  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — only hit when extra missing
        raise ImportError(
            "AsyncS3StorageBackend requires the `aioboto3` package. "
            "Install with: pip install 'genblaze-s3[async]' "
            "(or pip install aioboto3>=12,<13)."
        ) from exc
    return aioboto3


def _build_aio_config() -> Any:
    """Mirror :func:`genblaze_s3.backend._build_boto_config` for aiobotocore.

    ``aiobotocore.config.AioConfig`` is a subclass of ``botocore.config.Config``
    and accepts the same kwargs. Setting these on the async client matters
    most for B2 compatibility: ``request_checksum_calculation="when_required"``
    disables the boto3 >= 1.36 CRC32 trailer that breaks B2 (and other older
    S3-compatible endpoints). Without this, async ``aget``/``astream``/
    threadpool-delegated ``aput`` on B2 would silently fail or corrupt —
    a Phase 3 final-review BLOCKING bug.

    Lazy-imported so the bare ``async_backend`` module stays importable
    on minimal installs.
    """
    from aiobotocore.config import AioConfig  # type: ignore[import-not-found]

    return AioConfig(
        user_agent_extra=_USER_AGENT,
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=30,
        read_timeout=300,
        max_pool_connections=_MAX_POOL_CONNECTIONS,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


class AsyncS3StorageBackend:
    """Native-async S3 backend wrapping a sync :class:`S3StorageBackend`.

    Use as an async context manager — the aioboto3 client is opened on
    ``__aenter__`` and closed on ``__aexit__``::

        async with AsyncS3StorageBackend(bucket="b", region="us-west-004") as ab:
            data = await ab.aget("k")

    The wrapped sync backend is exposed as :attr:`sync` for callers
    that need the historical surface (cache info, lifecycle helpers,
    etc.) without leaving the async context.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        public_url_base: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        # Lazy-import the sync class to avoid module-load cycle and to
        # let users construct AsyncS3StorageBackend without paying for
        # the boto3 import unless they actually instantiate.
        from genblaze_s3.backend import S3StorageBackend

        self._sync = S3StorageBackend(
            bucket=bucket,
            endpoint_url=endpoint_url,
            region=region,
            public_url_base=public_url_base,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )
        self._aio_session: Any = None
        self._aio_client_ctx: Any = None
        self._aio_client: Any = None
        # Save args needed for the aioboto3 client construction inside
        # ``__aenter__``. Mirrors what ``S3StorageBackend._client_kwargs``
        # builds for sync boto3.
        self._aio_client_kwargs = self._build_aio_client_kwargs()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_sync(cls, sync_backend: S3StorageBackend) -> AsyncS3StorageBackend:
        """Construct an async backend that shares the sync backend's settings.

        Useful when an app already has a configured sync backend (e.g. via
        :meth:`S3StorageBackend.for_backblaze`) and wants to add an async
        client without re-passing every kwarg.

        **Carries preflight state forward**: ``_region_verified`` and
        the (possibly-auto-corrected) ``_region`` / ``_endpoint_url``
        from the source sync backend are copied to the new internal
        sync delegate. Without this, the first threadpool-delegated
        call (e.g. ``aput``) would re-issue ``HeadBucket`` even though
        the source was already verified — a redundant round-trip on
        every from_sync construction. Phase 3 review fix #4.
        """
        ab = cls(
            bucket=sync_backend._bucket,
            endpoint_url=sync_backend._endpoint_url,
            region=sync_backend._region,
            public_url_base=sync_backend._public_url_base,
            aws_access_key_id=sync_backend._aws_access_key_id,
            aws_secret_access_key=sync_backend._aws_secret_access_key,
        )
        # Forward preflight state so the internal sync delegate skips
        # a redundant HeadBucket. Note: _region and _endpoint_url may
        # have been rewritten by the source's auto-correct logic
        # (B2 region redirect) — copy the post-rewrite values.
        ab._sync._region_verified = sync_backend._region_verified
        ab._sync._region = sync_backend._region
        ab._sync._endpoint_url = sync_backend._endpoint_url
        ab._sync._preflight_error = sync_backend._preflight_error
        # Rebuild the aio kwargs since region/endpoint_url may have
        # changed via the copy-after-construction.
        ab._aio_client_kwargs = ab._build_aio_client_kwargs()
        return ab

    def _build_aio_client_kwargs(self) -> dict[str, Any]:
        """Mirror :meth:`S3StorageBackend._client_kwargs` for aioboto3.

        ``config`` is built lazily on ``__aenter__`` (when aiobotocore
        is guaranteed importable) rather than at construction time.
        Construction-time errors should not require the async extra
        to be installed.
        """
        kwargs: dict[str, Any] = {}
        if self._sync._endpoint_url:
            kwargs["endpoint_url"] = self._sync._endpoint_url
        if self._sync._region:
            kwargs["region_name"] = self._sync._region
        if self._sync._aws_access_key_id:
            kwargs["aws_access_key_id"] = self._sync._aws_access_key_id
        if self._sync._aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self._sync._aws_secret_access_key
        return kwargs

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AsyncS3StorageBackend:
        aioboto3 = _require_aioboto3()
        self._aio_session = aioboto3.Session()
        # Inject the AioConfig at __aenter__ time — aiobotocore is only
        # guaranteed importable when aioboto3 is. Mirrors the sync
        # BotoConfig (B2 checksum-trailer fix, timeouts, pool size).
        client_kwargs = dict(self._aio_client_kwargs)
        client_kwargs.setdefault("config", _build_aio_config())
        self._aio_client_ctx = self._aio_session.client("s3", **client_kwargs)
        self._aio_client = await self._aio_client_ctx.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Clear references FIRST so a failed inner __aexit__ doesn't
        # leave the backend holding a stale ctx that a re-enter would
        # orphan. Capture a local snapshot for the actual teardown call;
        # any exception from inner cleanup propagates naturally.
        ctx = self._aio_client_ctx
        self._aio_client_ctx = None
        self._aio_client = None
        self._aio_session = None
        if ctx is not None:
            await ctx.__aexit__(exc_type, exc, tb)

    @property
    def sync(self) -> S3StorageBackend:
        """The wrapped sync backend — exposed for callers that need
        non-async helpers (lifecycle, key utilities, etc.) without
        leaving the async context."""
        return self._sync

    def _require_open(self) -> Any:
        if self._aio_client is None:
            raise RuntimeError(
                "AsyncS3StorageBackend used outside of `async with` context. "
                "Wrap usage in `async with AsyncS3StorageBackend(...) as ab:`."
            )
        return self._aio_client

    # ------------------------------------------------------------------
    # Native async hot-path ops
    # ------------------------------------------------------------------

    async def aget(
        self,
        key: str,
        *,
        encryption: Encryption | None = None,
        progress: Callable[[TransferProgress], None] | None = None,
    ) -> bytes:
        """Native async download — ``await client.get_object`` then
        ``await Body.read()``.

        Parity with :meth:`S3StorageBackend.get`: tolerant to
        ``encryption=`` (SSE-C envelope passthrough) and
        ``progress=`` callback. Without progress, the entire body is
        read in one ``await Body.read()`` call (single allocation).
        With progress, the body is read in 1 MiB chunks and the
        callback fires per chunk with cumulative byte counts.
        """
        client = self._require_open()
        extra: dict[str, Any] = {}
        if encryption is not None:
            extra.update(encryption.to_get_extra_args())
        try:
            resp = await client.get_object(Bucket=self._sync._bucket, Key=key, **extra)
        except Exception as exc:
            raise classify_botocore_error(exc, operation="aget", key=key) from exc

        body = resp["Body"]
        if progress is None:
            try:
                return await body.read()
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    try:
                        await _maybe_await(close())
                    except Exception:  # noqa: BLE001, S110 — release is best-effort
                        pass

        # Progress path: 1 MiB chunked read with cumulative reporting.
        # Pre-allocate when ContentLength is known (matches the sync
        # path's memory profile from Phase 2 review fix #4).
        total = resp.get("ContentLength")
        try:
            if total is not None:
                buf = bytearray(total)
                pos = 0
                cumulative = 0
                while True:
                    chunk = await body.read(self._sync._GET_PROGRESS_CHUNK_SIZE)
                    if not chunk:
                        break
                    end = pos + len(chunk)
                    buf[pos:end] = chunk
                    pos = end
                    cumulative += len(chunk)
                    progress(
                        TransferProgress(
                            bytes_transferred=cumulative,
                            total_bytes=total,
                            operation="get",
                            key=key,
                        )
                    )
                return bytes(buf if pos == total else buf[:pos])
            buf = bytearray()
            cumulative = 0
            while True:
                chunk = await body.read(self._sync._GET_PROGRESS_CHUNK_SIZE)
                if not chunk:
                    break
                buf.extend(chunk)
                cumulative += len(chunk)
                progress(
                    TransferProgress(
                        bytes_transferred=cumulative,
                        total_bytes=None,
                        operation="get",
                        key=key,
                    )
                )
            return bytes(buf)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except Exception:  # noqa: BLE001, S110 — release is best-effort
                    pass

    async def astream(
        self,
        key: str,
        *,
        chunk_size: int = 8 * 1024 * 1024,
        encryption: Encryption | None = None,
        progress: Callable[[TransferProgress], None] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Native async streaming download — yields chunks as they arrive.

        This is the headline win of native async over the threadpool
        wrapper: the iterator is genuinely async (awaitable per
        chunk), and the underlying HTTP socket is non-blocking. The
        sync version's ``Iterator[bytes]`` would have to be drained
        into a queue before threadpool-wrapping into an
        ``AsyncIterator``, which is exactly the back-pressure-killing
        pattern Phase 0 deferred to here.

        Connection lifecycle on early exit: same caveat as the sync
        :meth:`S3StorageBackend.stream` — if the caller stops
        iterating mid-stream, the underlying HTTP connection is
        discarded rather than recycled.
        """
        if chunk_size < 1:
            raise ValueError(f"astream: chunk_size must be ≥ 1, got {chunk_size}")
        client = self._require_open()
        extra: dict[str, Any] = {}
        if encryption is not None:
            extra.update(encryption.to_get_extra_args())
        try:
            resp = await client.get_object(Bucket=self._sync._bucket, Key=key, **extra)
        except Exception as exc:
            raise classify_botocore_error(exc, operation="astream", key=key) from exc

        body = resp["Body"]
        total = resp.get("ContentLength")
        cumulative = 0
        try:
            async for chunk in body.iter_chunks(chunk_size=chunk_size):
                if not chunk:
                    continue
                cumulative += len(chunk)
                if progress is not None:
                    progress(
                        TransferProgress(
                            bytes_transferred=cumulative,
                            total_bytes=total,
                            operation="stream",
                            key=key,
                        )
                    )
                yield chunk
        finally:
            # PEP 525: ``await`` is safe in an async generator's ``finally``
            # even during ``aclose()`` — Python drives the coroutine through
            # the finally including nested awaits. Don't "simplify" by
            # dropping the await; doing so would leak the body close
            # coroutine on the version of aioboto3 where ``close()``
            # returns a coroutine.
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except Exception:  # noqa: BLE001, S110 — release is best-effort
                    pass

    # ------------------------------------------------------------------
    # Threadpool-delegated ops — historical surface for completeness.
    # Native versions are deferred to a follow-up sub-phase.
    # ------------------------------------------------------------------

    async def aput(self, key: str, data: bytes | Any, **kwargs: Any) -> str:
        """Threadpool-delegated put. Native multipart-aware aput is a
        follow-up — aioboto3's upload_fileobj integration is non-trivial
        and out of scope for this sub-phase."""
        return await asyncio.to_thread(self._sync.put, key, data, **kwargs)

    async def ahead(self, key: str, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._sync.head, key, **kwargs)

    async def alist(self, prefix: str = "", **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._sync.list, prefix, **kwargs)

    async def aexists(self, key: str) -> bool:
        return await asyncio.to_thread(self._sync.exists, key)

    async def adelete(self, key: str) -> None:
        await asyncio.to_thread(self._sync.delete, key)

    async def acopy(self, src_key: str, dst_key: str, **kwargs: Any) -> None:
        await asyncio.to_thread(self._sync.copy, src_key, dst_key, **kwargs)

    async def adelete_many(self, keys: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._sync.delete_many, keys, **kwargs)

    async def adelete_prefix(self, prefix: str, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._sync.delete_prefix, prefix, **kwargs)

    async def aget_range(self, key: str, *, offset: int, length: int, **kwargs: Any) -> bytes:
        return await asyncio.to_thread(
            self._sync.get_range, key, offset=offset, length=length, **kwargs
        )

    async def aget_url(self, key: str, **kwargs: Any) -> str:
        return await asyncio.to_thread(self._sync.get_url, key, **kwargs)

    async def aget_durable_url(self, key: str) -> str:
        return await asyncio.to_thread(self._sync.get_durable_url, key)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it's a coroutine; pass through otherwise.

    aioboto3's ``StreamingBody.close()`` returns a coroutine on some
    versions and ``None`` on others — the helper smooths over the
    version drift without forcing a hard pin.
    """
    if asyncio.iscoroutine(value):
        return await value
    return value


__all__ = ["AsyncS3StorageBackend"]
