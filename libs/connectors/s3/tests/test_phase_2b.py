"""Phase 2B regression tests — delete_many / delete_prefix on S3.

Covers the safety asymmetry (delete_many=False default,
delete_prefix=True default), batched chunking at 1000 keys, partial
failures via the per-key Errors array, page-streamed delete_prefix,
and the empty-prefix safeguard.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from genblaze_core.storage.types import DeleteError, DeleteResult

from tests.conftest import _FakeClientError

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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


def _list_response(keys, *, next_token=None):
    return {
        "Contents": [{"Key": k, "Size": 1, "LastModified": _NOW, "ETag": '"x"'} for k in keys],
        "IsTruncated": next_token is not None,
        "NextContinuationToken": next_token,
    }


# ---------------------------------------------------------------------------
# delete_many
# ---------------------------------------------------------------------------


class TestDeleteMany:
    def test_empty_list_no_call(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        result = backend.delete_many([])
        assert result == DeleteResult(deleted=(), errors=(), dry_run=False)
        mock_client.delete_objects.assert_not_called()

    def test_dry_run_returns_keys_without_call(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        result = backend.delete_many(["a", "b", "c"], dry_run=True)
        assert result.deleted == ("a", "b", "c")
        assert result.errors == ()
        assert result.dry_run is True
        mock_client.delete_objects.assert_not_called()

    def test_single_batch_under_1000(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": "a"}, {"Key": "b"}],
            "Errors": [],
        }
        result = backend.delete_many(["a", "b"])
        assert result.deleted == ("a", "b")
        assert result.errors == ()
        assert result.dry_run is False
        # One DeleteObjects call.
        mock_client.delete_objects.assert_called_once()
        kwargs = mock_client.delete_objects.call_args.kwargs
        assert kwargs["Bucket"] == "my-bucket"
        assert [o["Key"] for o in kwargs["Delete"]["Objects"]] == ["a", "b"]

    def test_chunks_at_1000(self, mock_boto3):
        """1500 keys → 2 batches of 1000 + 500."""
        backend, mock_client = _make_backend(mock_boto3)
        # Mock returns the same shape for both calls; we only care about call_count.
        mock_client.delete_objects.return_value = {"Deleted": [], "Errors": []}
        keys = [f"k-{i}" for i in range(1500)]
        backend.delete_many(keys)
        assert mock_client.delete_objects.call_count == 2
        first_batch = mock_client.delete_objects.call_args_list[0].kwargs["Delete"]["Objects"]
        second_batch = mock_client.delete_objects.call_args_list[1].kwargs["Delete"]["Objects"]
        assert len(first_batch) == 1000
        assert len(second_batch) == 500

    def test_per_key_errors_propagated(self, mock_boto3):
        """S3's Errors array maps to DeleteError instances on the result."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": "a"}],
            "Errors": [{"Key": "b", "Code": "AccessDenied", "Message": "forbidden"}],
        }
        result = backend.delete_many(["a", "b"])
        assert result.deleted == ("a",)
        assert result.errors == (DeleteError(key="b", code="AccessDenied", message="forbidden"),)
        assert result.all_succeeded is False

    def test_whole_batch_failure_attributed_per_key(self, mock_boto3):
        """If ``DeleteObjects`` raises, every key in the batch lands in errors
        — partial-failure callers stay consistent."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.delete_objects.side_effect = _FakeClientError(
            {"Error": {"Code": "ServiceUnavailable"}}, "DeleteObjects"
        )
        result = backend.delete_many(["a", "b"])
        assert result.deleted == ()
        assert len(result.errors) == 2
        assert {e.key for e in result.errors} == {"a", "b"}


# ---------------------------------------------------------------------------
# delete_prefix
# ---------------------------------------------------------------------------


class TestDeletePrefix:
    def test_empty_prefix_raises(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(ValueError, match="non-empty prefix"):
            backend.delete_prefix("")

    def test_whitespace_only_prefix_raises(self, mock_boto3):
        backend, _ = _make_backend(mock_boto3)
        with pytest.raises(ValueError, match="non-empty prefix"):
            backend.delete_prefix("   ")

    def test_dry_run_default_lists_only(self, mock_boto3):
        """Default ``dry_run=True`` walks list() but never deletes."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = _list_response(["k1", "k2", "k3"])
        result = backend.delete_prefix("run-")
        assert result.deleted == ("k1", "k2", "k3")
        assert result.dry_run is True
        mock_client.delete_objects.assert_not_called()

    def test_explicit_opt_out_actually_deletes(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = _list_response(["k1", "k2"])
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": "k1"}, {"Key": "k2"}],
            "Errors": [],
        }
        result = backend.delete_prefix("run-", dry_run=False)
        assert result.deleted == ("k1", "k2")
        assert result.errors == ()
        assert result.dry_run is False
        mock_client.delete_objects.assert_called_once()

    def test_streams_pages_doesnt_buffer_all_keys(self, mock_boto3):
        """Each list page triggers its own DeleteObjects — memory bounded
        even when matching prefix returns millions of keys.

        We model this with two pages and assert a delete call PER page
        (not a single delete after all pages collected).
        """
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.side_effect = [
            _list_response(["k1", "k2"], next_token="cursor-2"),
            _list_response(["k3"]),
        ]
        mock_client.delete_objects.side_effect = [
            {"Deleted": [{"Key": "k1"}, {"Key": "k2"}], "Errors": []},
            {"Deleted": [{"Key": "k3"}], "Errors": []},
        ]
        result = backend.delete_prefix("run-", dry_run=False)
        # Two list calls (page 1 then page 2 with continuation_token).
        assert mock_client.list_objects_v2.call_count == 2
        # Two delete_objects calls — one per page.
        assert mock_client.delete_objects.call_count == 2
        assert result.deleted == ("k1", "k2", "k3")

    def test_empty_prefix_match_returns_empty_result(self, mock_boto3):
        """Prefix that matches nothing → empty result, no deletes."""
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = _list_response([])
        result = backend.delete_prefix("nonexistent/", dry_run=False)
        assert result.deleted == ()
        assert result.errors == ()
        mock_client.delete_objects.assert_not_called()

    def test_partial_failures_propagate_through_pages(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.side_effect = [
            _list_response(["k1", "k2"], next_token="cursor-2"),
            _list_response(["k3"]),
        ]
        mock_client.delete_objects.side_effect = [
            {
                "Deleted": [{"Key": "k1"}],
                "Errors": [{"Key": "k2", "Code": "AccessDenied", "Message": "no"}],
            },
            {"Deleted": [{"Key": "k3"}], "Errors": []},
        ]
        result = backend.delete_prefix("run-", dry_run=False)
        assert result.deleted == ("k1", "k3")
        assert len(result.errors) == 1
        assert result.errors[0].key == "k2"

    def test_list_failure_mid_walk_returns_partial_result(self, mock_boto3):
        """**Phase 2 review fix #5:** if list() raises on page N, the
        already-deleted keys from pages 1..N-1 are preserved on the
        result, and a synthetic DeleteError("", "list_failed", …)
        records the failure. Caller sees partial progress instead
        of having the StorageError swallow the already-deleted state.
        """
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.side_effect = [
            _list_response(["k1", "k2"], next_token="cursor-2"),
            _FakeClientError({"Error": {"Code": "InternalError"}}, "ListObjectsV2"),
        ]
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": "k1"}, {"Key": "k2"}],
            "Errors": [],
        }

        result = backend.delete_prefix("run-", dry_run=False)
        # Page 1 deletes are preserved.
        assert result.deleted == ("k1", "k2")
        # Page 2 list failure surfaces as a synthetic error.
        assert len(result.errors) == 1
        synthetic = result.errors[0]
        assert synthetic.key == ""
        assert synthetic.code == "list_failed"
        assert "page 2" in synthetic.message


# ---------------------------------------------------------------------------
# Async pairs
# ---------------------------------------------------------------------------


class TestAsyncDeletes:
    def test_adelete_many_delegates(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": "a"}],
            "Errors": [],
        }
        result = asyncio.run(backend.adelete_many(["a"]))
        assert result.deleted == ("a",)

    def test_adelete_prefix_delegates(self, mock_boto3):
        backend, mock_client = _make_backend(mock_boto3)
        mock_client.list_objects_v2.return_value = _list_response(["k1"])
        result = asyncio.run(backend.adelete_prefix("run-"))
        # Default dry_run=True propagates through async pair.
        assert result.dry_run is True
        assert result.deleted == ("k1",)
