"""Tests for StorageBackend ABC and KeyStrategy."""

import pytest
from genblaze_core.storage.base import KeyStrategy, StorageBackend


class TestKeyStrategy:
    def test_hierarchical_value(self):
        assert KeyStrategy.HIERARCHICAL == "hierarchical"

    def test_content_addressable_value(self):
        assert KeyStrategy.CONTENT_ADDRESSABLE == "content_addressable"

    def test_all_values(self):
        assert set(KeyStrategy) == {"hierarchical", "content_addressable"}


class TestStorageBackendABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            StorageBackend()

    def test_subclass_must_implement_all(self):
        """Subclass missing abstract methods should raise TypeError."""

        class Partial(StorageBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                return ""

        with pytest.raises(TypeError):
            Partial()

    def test_subclass_missing_get_durable_url_rejected(self):
        """Subclass missing only get_durable_url must not instantiate.

        get_durable_url is the security boundary that keeps presigned URLs
        out of persisted manifests — the ABC enforces it.
        """

        class MissingDurable(StorageBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                return ""

            def get(self, key):
                return b""

            def exists(self, key):
                return False

            def delete(self, key):
                pass

            def get_url(self, key, *, expires_in=3600):
                return ""

        with pytest.raises(TypeError):
            MissingDurable()

    def test_complete_subclass_instantiates(self):
        """A subclass implementing all abstract methods should work."""

        class Complete(StorageBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                return f"url://{key}"

            def get(self, key):
                return b""

            def exists(self, key):
                return False

            def delete(self, key):
                pass

            def get_url(self, key, *, expires_in=3600):
                return f"url://{key}"

            def get_durable_url(self, key):
                return f"url://{key}"

        backend = Complete()
        assert backend.put("k", b"data") == "url://k"
        assert backend.exists("k") is False
        assert backend.get_durable_url("k") == "url://k"

    def test_close_default_is_noop(self):
        """Default close() should not raise."""

        class Minimal(StorageBackend):
            def put(self, key, data, *, content_type=None, metadata=None, extra_args=None):
                return ""

            def get(self, key):
                return b""

            def exists(self, key):
                return False

            def delete(self, key):
                pass

            def get_url(self, key, *, expires_in=3600):
                return ""

            def get_durable_url(self, key):
                return ""

        backend = Minimal()
        backend.close()  # should not raise
