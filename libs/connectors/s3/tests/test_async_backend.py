"""Phase 3 regression tests — AsyncS3StorageBackend native async surface.

aioboto3 is an optional dep that isn't installed in the dev env by
default. Rather than skip the suite when it's missing, we mock the
aioboto3 module surface in ``sys.modules`` (same pattern the parent
conftest uses for boto3). The mocked client lets us verify wire-shape
expectations end-to-end without a real network call OR a real
aioboto3 install.

Coverage:

* Lazy import — ``ImportError`` with the extras hint when aioboto3 is
  missing entirely (no mock).
* Lifecycle — ``__aenter__`` opens an aioboto3 client; ``__aexit__``
  closes it; methods called outside the context raise ``RuntimeError``.
* Native ``aget`` — single-shot read; chunked-progress path with
  pre-allocated bytearray and unknown-total fallback.
* Native ``astream`` — async-iter chunks via ``Body.iter_chunks``;
  progress callback per chunk; chunk_size validation.
* Threadpool-delegated methods — ``aput``, ``ahead``, ``alist``,
  ``adelete_many``, etc. — verify they hit the wrapped sync backend.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.types import TransferProgress

# ---------------------------------------------------------------------------
# Helpers — mock aioboto3 surface
# ---------------------------------------------------------------------------


class _FakeAioStreamingBody:
    """In-memory async-streaming body with iter_chunks support.

    Mirrors what aioboto3's ``StreamingBody`` exposes for our purposes:
    ``await body.read([n])`` and ``async for chunk in
    body.iter_chunks(chunk_size=...)``. ``close()`` is a no-op coroutine.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    async def iter_chunks(self, chunk_size: int = 8 * 1024 * 1024) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.read(chunk_size)
            if not chunk:
                return
            yield chunk

    async def close(self) -> None:
        pass


class _FakeAioS3Client:
    """Minimal aioboto3 S3 client surface — only the calls we actually use."""

    def __init__(self) -> None:
        self.get_object = AsyncMock()
        self._closed = False

    async def __aenter__(self) -> _FakeAioS3Client:
        return self

    async def __aexit__(self, *args, **kwargs) -> None:
        self._closed = True


class _FakeAioSession:
    """Stand-in for ``aioboto3.Session()``."""

    def __init__(self) -> None:
        self.client_factory = MagicMock(return_value=_FakeAioS3Client())

    def client(self, _service: str, **_kwargs) -> _FakeAioS3Client:
        return self.client_factory.return_value


def _install_aioboto3_mock() -> _FakeAioSession:
    """Inject fake ``aioboto3`` and ``aiobotocore.config`` modules into
    ``sys.modules`` and return the session that
    ``AsyncS3StorageBackend`` will obtain via ``aioboto3.Session()``.

    ``aiobotocore.config.AioConfig`` is needed by Phase 3 review fix #1
    (mirrors the sync BotoConfig for B2 checksum-trailer compat). In a
    real install, aiobotocore is a transitive dep of aioboto3; for the
    test mock we ship a captured-kwargs stand-in so tests can assert
    the right config knobs were passed.
    """
    session = _FakeAioSession()
    fake_aioboto3 = MagicMock()
    fake_aioboto3.Session.return_value = session
    sys.modules["aioboto3"] = fake_aioboto3

    # Mock aiobotocore.config.AioConfig — capture the kwargs so tests
    # can assert the right knobs (request_checksum_calculation, etc.)
    # were forwarded.
    fake_aiobotocore = MagicMock()
    fake_aiobotocore_config = MagicMock()

    class _CapturedAioConfig:
        last_kwargs: dict = {}

        def __init__(self, **kwargs):
            _CapturedAioConfig.last_kwargs = kwargs
            self.kwargs = kwargs

    fake_aiobotocore_config.AioConfig = _CapturedAioConfig
    fake_aiobotocore.config = fake_aiobotocore_config
    sys.modules["aiobotocore"] = fake_aiobotocore
    sys.modules["aiobotocore.config"] = fake_aiobotocore_config
    session.aio_config_capture = _CapturedAioConfig  # type: ignore[attr-defined]
    return session


@pytest.fixture
def aio_session():
    """Per-test mocked aioboto3 — installed before instantiation, removed
    after. Side-effects on ``sys.modules`` are scoped to the test."""
    # Pop any prior import so ``_require_aioboto3`` re-resolves to our
    # mock cleanly.
    for cached in ("aioboto3", "aiobotocore", "aiobotocore.config"):
        sys.modules.pop(cached, None)
    session = _install_aioboto3_mock()
    yield session
    for cached in ("aioboto3", "aiobotocore", "aiobotocore.config"):
        sys.modules.pop(cached, None)


# ---------------------------------------------------------------------------
# Lazy import + extras-hint ImportError
# ---------------------------------------------------------------------------


class TestLazyImport:
    def test_module_imports_without_aioboto3_installed(self, mock_boto3):
        """Importing AsyncS3StorageBackend itself must NOT require
        aioboto3 — only instantiation should."""
        sys.modules.pop("aioboto3", None)
        # Make `import aioboto3` fail.
        with patch.dict(sys.modules, {"aioboto3": None}):
            from genblaze_s3.async_backend import AsyncS3StorageBackend

            # Construction is fine — aioboto3 only checked on __aenter__.
            backend = AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            )
            assert backend.sync._bucket == "b"

    def test_aenter_without_aioboto3_raises_import_error(self, mock_boto3):
        sys.modules.pop("aioboto3", None)
        with patch.dict(sys.modules, {"aioboto3": None}):
            from genblaze_s3.async_backend import AsyncS3StorageBackend

            backend = AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            )
            with pytest.raises(ImportError, match=r"genblaze-s3\[async\]"):
                asyncio.run(backend.__aenter__())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_methods_outside_context_raise(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        backend = AsyncS3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.example.test",
            region="us-west-004",
        )
        with pytest.raises(RuntimeError, match="async with"):
            asyncio.run(backend.aget("k"))

    def test_aenter_aexit_lifecycle(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                # Inside context: client is set.
                assert ab._aio_client is not None

        asyncio.run(run())

    def test_from_sync_constructor_shares_settings(self, mock_boto3, aio_session):
        """from_sync borrows bucket/region/credentials from a configured
        sync backend — common pattern for apps adding async to an
        existing setup."""
        from genblaze_s3 import S3StorageBackend
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        sync = S3StorageBackend(
            bucket="my-bucket",
            endpoint_url="https://s3.example.test",
            region="eu-central-003",
            aws_access_key_id="AKIA",
            aws_secret_access_key="S",  # noqa: S106 — test fixture
        )
        async_backend = AsyncS3StorageBackend.from_sync(sync)
        assert async_backend.sync._bucket == "my-bucket"
        assert async_backend._aio_client_kwargs["region_name"] == "eu-central-003"
        assert async_backend._aio_client_kwargs["aws_access_key_id"] == "AKIA"


# ---------------------------------------------------------------------------
# Native aget
# ---------------------------------------------------------------------------


class TestNativeAget:
    def _make_response(self, data: bytes, *, content_length: int | None = None):
        body = _FakeAioStreamingBody(data)
        resp: dict = {"Body": body}
        if content_length is not None:
            resp["ContentLength"] = content_length
        return resp

    def test_aget_single_shot_no_progress(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        client.get_object.return_value = self._make_response(b"hello", content_length=5)

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                return await ab.aget("k")

        out = asyncio.run(run())
        assert out == b"hello"
        client.get_object.assert_awaited_once()
        kwargs = client.get_object.call_args.kwargs
        assert kwargs["Bucket"] == "b"
        assert kwargs["Key"] == "k"

    def test_aget_with_progress_chunks_and_fires_per_chunk(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        # 10-byte body, ContentLength known → pre-allocated bytearray path.
        client.get_object.return_value = self._make_response(b"0123456789", content_length=10)

        events: list[TransferProgress] = []

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                return await ab.aget("k", progress=events.append)

        out = asyncio.run(run())
        assert out == b"0123456789"
        # At least one progress event with cumulative=10 by the end.
        assert events[-1].bytes_transferred == 10
        assert events[-1].total_bytes == 10
        assert events[-1].operation == "get"

    def test_aget_progress_unknown_total_grows_buffer(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        # No ContentLength → growing-bytearray path.
        client.get_object.return_value = self._make_response(b"abcdef")

        events: list[TransferProgress] = []

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                return await ab.aget("k", progress=events.append)

        assert asyncio.run(run()) == b"abcdef"
        assert events[-1].bytes_transferred == 6
        assert events[-1].total_bytes is None

    def test_aget_failure_wrapped_as_storage_error(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        client.get_object.side_effect = RuntimeError("network blip")

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                with pytest.raises(StorageError, match=r"aget.*failed"):
                    await ab.aget("k")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Native astream
# ---------------------------------------------------------------------------


class TestNativeAstream:
    def test_astream_yields_chunks(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        body = _FakeAioStreamingBody(b"a" * 10)
        client.get_object.return_value = {"Body": body, "ContentLength": 10}

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                chunks = []
                async for c in ab.astream("k", chunk_size=4):
                    chunks.append(c)
                return chunks

        chunks = asyncio.run(run())
        # Body of 10 split at chunk_size=4 → [4, 4, 2].
        assert chunks == [b"a" * 4, b"a" * 4, b"a" * 2]

    def test_astream_progress_fires_per_chunk(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        client = aio_session.client_factory.return_value
        body = _FakeAioStreamingBody(b"01234567")  # 8 bytes
        client.get_object.return_value = {"Body": body, "ContentLength": 8}

        events: list[TransferProgress] = []

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                async for _ in ab.astream("k", chunk_size=3, progress=events.append):
                    pass

        asyncio.run(run())
        # 3, 3, 2 bytes → cumulative 3, 6, 8.
        assert [e.bytes_transferred for e in events] == [3, 6, 8]
        assert all(e.operation == "stream" for e in events)
        assert all(e.total_bytes == 8 for e in events)

    def test_astream_chunk_size_validation(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                # Trigger generator iteration to hit validation.
                gen = ab.astream("k", chunk_size=0)
                with pytest.raises(ValueError, match="chunk_size must be"):
                    await gen.__anext__()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Threadpool-delegated methods
# ---------------------------------------------------------------------------


class TestThreadpoolDelegation:
    def test_aput_delegates_to_sync(self, mock_boto3, aio_session):
        """aput threadpool-wraps the sync put. Native multipart-aware
        aput is a follow-up sub-phase."""
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                ab.sync._region_verified = True  # skip preflight
                return await ab.aput("k", b"data")

        # The sync backend's mocked boto3 client returns a MagicMock for
        # upload_fileobj; aput returns the storage key per Phase 1B contract.
        result = asyncio.run(run())
        assert result == "k"

    def test_ahead_delegates(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                ab.sync._region_verified = True
                # head_object isn't set on the mock client → mocked returns
                # MagicMock, which doesn't quack like the boto response.
                # But ahead just calls sync.head; sync.head wraps client.
                # We're testing delegation, not full output here.
                from tests.conftest import _FakeClientError

                ab.sync._client.head_object.side_effect = _FakeClientError(
                    {"Error": {"Code": "404"}}, "HeadObject"
                )
                return await ab.ahead("missing")

        # 404 → None (parity with sync).
        assert asyncio.run(run()) is None

    def test_alist_delegates(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            ) as ab:
                ab.sync._region_verified = True
                ab.sync._client.list_objects_v2.return_value = {
                    "Contents": [],
                    "IsTruncated": False,
                }
                return await ab.alist()

        page = asyncio.run(run())
        assert page.entries == ()
        assert page.next_token is None


# ---------------------------------------------------------------------------
# Phase 3 review-fix regression tests
# ---------------------------------------------------------------------------


class TestReviewFix1AioConfig:
    """**Fix #1 (BLOCKING):** the aioboto3 client now receives an
    ``AioConfig`` mirroring the sync ``BotoConfig`` so B2 endpoints
    that reject the boto3 >= 1.36 CRC32 trailer continue to work
    on async paths."""

    def test_aio_config_carries_checksum_when_required(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        async def run():
            async with AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.us-west-004.backblazeb2.com",
                region="us-west-004",
            ):
                pass

        asyncio.run(run())
        # Check that AioConfig was constructed with the B2-critical knobs.
        cfg = aio_session.aio_config_capture.last_kwargs
        assert cfg["request_checksum_calculation"] == "when_required"
        assert cfg["response_checksum_validation"] == "when_required"
        # And the same timeout / pool / user-agent values the sync path uses.
        assert cfg["connect_timeout"] == 30
        assert cfg["read_timeout"] == 300
        assert cfg["max_pool_connections"] == 20
        assert cfg["user_agent_extra"].startswith("b2ai-genblaze/")


class TestReviewFix3AexitOrdering:
    """**Fix #3 (IMPORTANT):** if the inner aioboto3 client's
    ``__aexit__`` raises, the backend still clears its references so
    a subsequent re-enter is safe (no orphaned ctx)."""

    def test_aexit_clears_state_even_when_inner_exit_raises(self, mock_boto3, aio_session):
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        # Wire the fake client's __aexit__ to raise.
        client = aio_session.client_factory.return_value

        async def boom_exit(*args, **kwargs):
            raise RuntimeError("aiohttp connector teardown failed")

        client.__aexit__ = boom_exit

        async def run():
            ab = AsyncS3StorageBackend(
                bucket="b",
                endpoint_url="https://s3.example.test",
                region="us-west-004",
            )
            await ab.__aenter__()
            assert ab._aio_client is not None
            with pytest.raises(RuntimeError, match="connector teardown failed"):
                await ab.__aexit__(None, None, None)
            # State cleared even though inner exit raised — re-enter is safe.
            assert ab._aio_client is None
            assert ab._aio_client_ctx is None
            assert ab._aio_session is None

        asyncio.run(run())


class TestReviewFix4FromSyncCarriesPreflight:
    """**Fix #4 (IMPORTANT):** ``from_sync`` now carries
    ``_region_verified`` and the (possibly-auto-corrected)
    ``_region``/``_endpoint_url`` from the source sync backend so
    the first threadpool-delegated call doesn't re-issue HeadBucket."""

    def test_from_sync_inherits_region_verified(self, mock_boto3, aio_session):
        from genblaze_s3 import S3StorageBackend
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        sync = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        # Caller already verified region — e.g. via a prior put/get.
        sync._region_verified = True

        async_backend = AsyncS3StorageBackend.from_sync(sync)
        # The internal sync delegate inherits the verified flag.
        assert async_backend.sync._region_verified is True

    def test_from_sync_inherits_auto_corrected_region(self, mock_boto3, aio_session):
        """If the source sync backend was auto-corrected to a different
        region (B2 redirect), the rewritten endpoint propagates."""
        from genblaze_s3 import S3StorageBackend
        from genblaze_s3.async_backend import AsyncS3StorageBackend

        sync = S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region="us-west-004",
        )
        # Simulate B2's auto-correct mid-flight: source's first call
        # got a 301 redirect and rewrote endpoint/region in place.
        sync._region_verified = True
        sync._region = "us-east-005"
        sync._endpoint_url = "https://s3.us-east-005.backblazeb2.com"

        async_backend = AsyncS3StorageBackend.from_sync(sync)
        assert async_backend.sync._region == "us-east-005"
        assert async_backend.sync._endpoint_url == "https://s3.us-east-005.backblazeb2.com"
        # The aio kwargs reflect the corrected endpoint, not the original.
        assert (
            async_backend._aio_client_kwargs["endpoint_url"]
            == "https://s3.us-east-005.backblazeb2.com"
        )
        assert async_backend._aio_client_kwargs["region_name"] == "us-east-005"
