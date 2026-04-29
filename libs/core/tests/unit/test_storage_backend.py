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
                # Contract change: put() returns the storage key, not a URL.
                return key

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
        # put() returns the storage key (not a URL) — callers compose with
        # get_durable_url for the persistable URL form.
        assert backend.put("k", b"data") == "k"
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

    def test_key_from_url_default_raises_not_implemented(self):
        """Default key_from_url raises so 'backend doesn't implement' is
        distinct from 'this URL doesn't belong to me' (None). Conflating
        the two would make foreign-URL routing silently swallow bugs."""

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
        with pytest.raises(NotImplementedError, match="key_from_url"):
            backend.key_from_url("https://example.com/k")
