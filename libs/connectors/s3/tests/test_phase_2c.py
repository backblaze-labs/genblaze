"""Phase 2C regression tests — progress callbacks + per-put Object Lock.

Covers:

* **put progress** — boto3 ``Callback=`` adapter accumulates deltas to
  cumulative; total_bytes inferred for ``bytes`` payloads; single-PUT
  path silently skips progress (no Callback param on put_object).
* **get progress** — chunked-read path when callback is set; fast
  ``body.read()`` path otherwise.
* **stream progress** — fires per yielded chunk with cumulative count.
* **per-put Object Lock** — merges via ``to_extra_args``; conflict with
  overlapping ``extra_args`` raises ``ValueError`` (mirrors SSE guard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from genblaze_core.storage.base import ObjectLockConfig
from genblaze_core.storage.types import TransferProgress


def _make_backend(mock_boto3_mod, **kwargs):
    from genblaze_s3.backend import S3StorageBackend

    mock_client = MagicMock()
    mock_boto3_mod.client.return_value = mock_client
    defaults = {
        "bucket": "my-bucket",
        "endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "region": "us-west-004",
    }
    defaults.update(kwargs)
    backend = S3StorageBackend(**defaults)
    backend._region_verified = True
    return backend, mock_client


# ---------------------------------------------------------------------------
# put progress
# ---------------------------------------------------------------------------


class TestPutProgress:
    def test_no_progress_skips_callback(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        backend.put("k", b"data")
        # Callback kwarg should be None when caller didn't pass progress.
        kwargs = mock_client.upload_fileobj.call_args.kwargs
        assert kwargs.get("Callback") is None

    def test_progress_adapter_accumulates_deltas_to_cumulative(self, mock_boto3):
        """boto3 calls Callback with delta bytes per chunk; the adapter
        forwards cumulative totals to the user-provided callback."""
        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []

        def on_progress(p: TransferProgress) -> None:
            events.append(p)

        # Capture the boto Callback so we can simulate boto3 calling it.
        backend.put("k", b"x" * 100, progress=on_progress)
        boto_cb = mock_client.upload_fileobj.call_args.kwargs["Callback"]

        # Simulate three multipart progress reports from boto3.
        boto_cb(40)
        boto_cb(30)
        boto_cb(30)

        assert [e.bytes_transferred for e in events] == [40, 70, 100]
        assert all(e.total_bytes == 100 for e in events)
        assert all(e.operation == "put" for e in events)
        assert all(e.key == "k" for e in events)

    def test_total_bytes_inferred_from_bytes_payload(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []
        backend.put("k", b"abcdef", progress=events.append)
        boto_cb = mock_client.upload_fileobj.call_args.kwargs["Callback"]
        boto_cb(6)
        assert events[0].total_bytes == 6

    def test_total_bytes_inferred_from_bytesio_payload(self, mock_boto3):
        import io

        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []
        backend.put("k", io.BytesIO(b"hello!"), progress=events.append)
        boto_cb = mock_client.upload_fileobj.call_args.kwargs["Callback"]
        boto_cb(6)
        assert events[0].total_bytes == 6

    def test_total_bytes_honors_bytesio_position(self, mock_boto3):
        """Phase 2 review fix #2 — _data_size now subtracts tell() so a
        partially-consumed BytesIO reports the correct remaining
        byte count, not the full buffer length."""
        import io

        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []
        buf = io.BytesIO(b"0123456789")  # 10 bytes
        buf.read(3)  # advance position to 3 — only 7 bytes remain
        backend.put("k", buf, progress=events.append)
        boto_cb = mock_client.upload_fileobj.call_args.kwargs["Callback"]
        boto_cb(7)
        # Pre-fix this would have been 10 (full buffer length).
        assert events[0].total_bytes == 7

    def test_progress_callback_is_thread_safe(self, mock_boto3):
        """Phase 2 review fix #1 — concurrent boto3 part workers must not
        drop deltas. The lock inside ``_adapt_progress_to_boto3_callback``
        serializes the read-modify-write so cumulative is always
        consistent. Stress-test by hammering the callback from multiple
        threads and asserting the final cumulative matches the sum
        of all deltas."""
        import threading

        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []
        backend.put("k", b"x" * 10_000, progress=events.append)
        boto_cb = mock_client.upload_fileobj.call_args.kwargs["Callback"]

        # 8 threads × 1000 calls each × delta=1 → expected 8000.
        def worker():
            for _ in range(1000):
                boto_cb(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Last event's bytes_transferred must equal total deltas
        # (8 × 1000 = 8000). Without the lock, races drop counts under
        # CPython's GIL-but-not-bytecode-atomic semantics.
        assert events[-1].bytes_transferred == 8000

    def test_total_bytes_none_for_unknown_stream(self, mock_boto3):
        """Arbitrary BinaryIO (not bytes / not BytesIO) has unknown total."""

        class _ArbitraryStream:
            def read(self, n=-1):
                return b""

        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []
        backend.put("k", _ArbitraryStream(), progress=events.append)  # type: ignore[arg-type]
        boto_cb = mock_client.upload_fileobj.call_args.kwargs["Callback"]
        boto_cb(10)
        assert events[0].total_bytes is None

    def test_single_put_path_silently_skips_progress(self, mock_boto3):
        """When caller pins ChecksumSHA256, put routes to put_object — boto3's
        single-PUT API doesn't accept Callback, so progress is silently
        skipped rather than raising."""
        backend, mock_client = _make_backend(mock_boto3)
        events: list[TransferProgress] = []
        backend.put(
            "k",
            b"data",
            extra_args={"ChecksumSHA256": "abc=="},
            progress=events.append,
        )
        # put_object got called (single-PUT path) — and was NOT given Callback.
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert "Callback" not in call_kwargs
        # No upload_fileobj call.
        mock_client.upload_fileobj.assert_not_called()
        # And no progress events fired (no opportunity).
        assert events == []


# ---------------------------------------------------------------------------
# get progress
# ---------------------------------------------------------------------------


class TestGetProgress:
    def test_no_progress_uses_fast_path(self, mock_boto3):
        """Without progress=, get() uses single body.read() — fastest path."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.return_value = b"big-payload"
        mock_client.get_object.return_value = {"Body": body, "ContentLength": 11}
        out = backend.get("k")
        assert out == b"big-payload"
        # Single read() call; no chunk loop.
        assert body.read.call_count == 1

    def test_progress_chunks_and_fires_per_chunk(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        # Simulate four 1 MiB reads then EOF.
        body.read.side_effect = [b"a" * 1024, b"b" * 512, b""]
        mock_client.get_object.return_value = {"Body": body, "ContentLength": 1536}

        events: list[TransferProgress] = []
        out = backend.get("k", progress=events.append)
        assert out == b"a" * 1024 + b"b" * 512
        assert [e.bytes_transferred for e in events] == [1024, 1536]
        assert all(e.total_bytes == 1536 for e in events)
        assert all(e.operation == "get" for e in events)

    def test_progress_total_none_when_content_length_missing(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b"x", b""]
        mock_client.get_object.return_value = {"Body": body}
        events: list[TransferProgress] = []
        backend.get("k", progress=events.append)
        assert events[0].total_bytes is None

    def test_progress_path_pre_allocates_when_total_known(self, mock_boto3):
        """**Phase 2 review fix #4:** when ContentLength is known, the
        chunked path pre-allocates a single bytearray sized to the
        total (avoids the list[bytes] + b"".join intermediate which
        had ~2× peak memory and one Python object per chunk).

        Verifying the output matches what we'd expect — the optimization
        must be transparent to callers."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        # 3 chunks of distinct content; ContentLength matches sum.
        body.read.side_effect = [b"abcd", b"ef", b"ghij", b""]
        mock_client.get_object.return_value = {"Body": body, "ContentLength": 10}
        events: list[TransferProgress] = []
        out = backend.get("k", progress=events.append)
        assert out == b"abcdefghij"
        # Cumulative progress at end matches total.
        assert events[-1].bytes_transferred == 10
        assert events[-1].total_bytes == 10

    def test_progress_path_truncates_when_content_length_overstated(self, mock_boto3):
        """Defensive guard: if Content-Length overstated the actual body
        length (rare but observed on some S3-compat endpoints during
        partial-write windows), return only the bytes actually read —
        never trailing zero-bytes from the pre-allocated buffer."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        # ContentLength claims 100, but body only delivers 5.
        body.read.side_effect = [b"hello", b""]
        mock_client.get_object.return_value = {"Body": body, "ContentLength": 100}
        events: list[TransferProgress] = []
        out = backend.get("k", progress=events.append)
        # Truncated to actual bytes — not the pre-allocated 100-byte buffer.
        assert out == b"hello"

    def test_progress_path_unknown_total_grows_buffer(self, mock_boto3):
        """When ContentLength is missing, the impl falls back to a growing
        bytearray (no pre-allocation possible). Output identical."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b"abc", b"def", b""]
        mock_client.get_object.return_value = {"Body": body}  # no ContentLength
        events: list[TransferProgress] = []
        out = backend.get("k", progress=events.append)
        assert out == b"abcdef"
        assert all(e.total_bytes is None for e in events)


# ---------------------------------------------------------------------------
# stream progress
# ---------------------------------------------------------------------------


class TestStreamProgress:
    def test_progress_fires_per_yielded_chunk(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b"a", b"bb", b"ccc", b""]
        mock_client.get_object.return_value = {"Body": body, "ContentLength": 6}

        events: list[TransferProgress] = []
        chunks = list(backend.stream("k", chunk_size=4, progress=events.append))

        assert chunks == [b"a", b"bb", b"ccc"]
        # Cumulative: 1, 3, 6.
        assert [e.bytes_transferred for e in events] == [1, 3, 6]
        assert all(e.total_bytes == 6 for e in events)
        assert all(e.operation == "stream" for e in events)

    def test_no_progress_doesnt_call_callback(self, mock_boto3):
        """Without progress=, stream() doesn't even allocate a callback path."""
        backend, mock_client = _make_backend(mock_boto3)
        body = MagicMock()
        body.read.side_effect = [b"a", b""]
        mock_client.get_object.return_value = {"Body": body}
        # Just iterating without progress= should not raise.
        assert list(backend.stream("k")) == [b"a"]


# ---------------------------------------------------------------------------
# Per-put Object Lock
# ---------------------------------------------------------------------------


class TestPerPutObjectLock:
    def _retain_until(self):
        return datetime.now(UTC) + timedelta(days=1)

    def test_object_lock_extra_args_merged_into_put(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        retain = self._retain_until()
        backend.put("k", b"data", object_lock=ObjectLockConfig(retain_until=retain))
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["ObjectLockMode"] == "GOVERNANCE"
        assert extra["ObjectLockRetainUntilDate"] == retain

    def test_object_lock_compliance_mode_passed_through(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        retain = self._retain_until()
        backend.put(
            "k",
            b"data",
            object_lock=ObjectLockConfig(retain_until=retain, mode="COMPLIANCE"),
        )
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["ObjectLockMode"] == "COMPLIANCE"

    def test_object_lock_overlap_with_extra_args_raises(self, mock_boto3):
        """Caller passes both object_lock= AND extra_args with an overlapping
        Object Lock key → fail upfront. Mirrors SSE conflict guard."""
        backend, _ = _make_backend(mock_boto3)
        retain = self._retain_until()
        with pytest.raises(ValueError, match="Object Lock envelope conflict"):
            backend.put(
                "k",
                b"data",
                object_lock=ObjectLockConfig(retain_until=retain),
                extra_args={"ObjectLockMode": "COMPLIANCE"},
            )

    def test_object_lock_legal_hold_status_overlap_also_raises(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        retain = self._retain_until()
        with pytest.raises(ValueError, match="Object Lock envelope conflict"):
            backend.put(
                "k",
                b"data",
                object_lock=ObjectLockConfig(retain_until=retain),
                extra_args={"ObjectLockLegalHoldStatus": "ON"},
            )

    def test_non_lock_extra_args_compose_with_object_lock(self, mock_boto3):
        """Caller can still pass non-lock extra_args alongside object_lock=."""
        backend, mock_client = _make_backend(mock_boto3)
        retain = self._retain_until()
        backend.put(
            "k",
            b"data",
            object_lock=ObjectLockConfig(retain_until=retain),
            extra_args={"CacheControl": "private, max-age=3600"},
        )
        extra = mock_client.upload_fileobj.call_args.kwargs["ExtraArgs"]
        assert extra["CacheControl"] == "private, max-age=3600"
        assert extra["ObjectLockMode"] == "GOVERNANCE"
