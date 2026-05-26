"""Tests for ``ObjectStorageSink(asset_url_policy=...)``.

Closes the 2026-05-23 feedback batch item 5: private buckets shipped
durable-only ``asset.url`` values that 403 in browsers. The fix is the
``asset_url_policy`` kwarg + a module-level WARN when the backend has no
``public_url_base``. ``URLPolicy.PRESIGNED`` is intentionally rejected
on the sink — manifests must not carry SigV4 URLs (they decay before
the manifest does, breaking provenance).

The probe used to verify backend-agnostic behavior is a stub class that
deliberately does NOT carry a ``public_url_base`` attribute. The sink's
sentinel-based lookup (``getattr(backend, "public_url_base",
_PUBLIC_URL_BASE_MISSING)``) must skip the WARN cleanly in that case
while still firing for the attribute-present-but-falsy case (None or
empty string).
"""

from __future__ import annotations

import logging

import pytest
from genblaze_core.storage.base import StorageBackend
from genblaze_core.storage.sink import ObjectStorageSink
from genblaze_core.storage.url_policy import URLPolicy, URLPolicyError


class _S3ShapedBackend(StorageBackend):
    """Minimal stub mimicking S3StorageBackend's public-url attribute.

    Carries ``public_url_base`` (the attribute the sink inspects) plus
    a ``_bucket`` field the sink reads when emitting the WARN.
    """

    def __init__(self, bucket: str, *, public_url_base: str | None = None):
        self._bucket = bucket
        self.public_url_base = public_url_base

    # The abstract surface — sink construction never exercises these,
    # so trivial stubs are fine.
    def put(self, key, data, **_):  # pragma: no cover — not exercised
        return f"mem://{key}"

    def get(self, key):  # pragma: no cover
        return b""

    def exists(self, key):  # pragma: no cover
        return False

    def delete(self, key):  # pragma: no cover
        pass

    def get_url(self, key, *, expires_in=3600):  # pragma: no cover
        return f"https://signed/{key}"

    def get_durable_url(self, key):  # pragma: no cover
        return f"https://durable/{key}"


class _BackendWithoutPublicUrlAttr(StorageBackend):
    """Backend that does NOT declare a ``public_url_base`` attribute at all.

    Verifies the sink's ``hasattr`` guard: AUTO + no attribute should
    skip the WARN entirely (we don't know whether durable URLs are
    appropriate for this backend, so don't second-guess).
    """

    def put(self, key, data, **_):  # pragma: no cover
        return f"mem://{key}"

    def get(self, key):  # pragma: no cover
        return b""

    def exists(self, key):  # pragma: no cover
        return False

    def delete(self, key):  # pragma: no cover
        pass

    def get_url(self, key, *, expires_in=3600):  # pragma: no cover
        return f"https://signed/{key}"

    def get_durable_url(self, key):  # pragma: no cover
        return f"https://durable/{key}"


@pytest.fixture(autouse=True)
def _clear_warn_guard():
    """Reset the module-level WARN-suppression set between tests.

    Without this, test ordering would leak state — a test that warns
    once would let the next test (asserting "warns once") fail because
    the bucket-policy pair would already be in the set.
    """
    from genblaze_core.storage.sink import _warned_durable_url

    _warned_durable_url.clear()
    yield
    _warned_durable_url.clear()


# ---------------------------------------------------------------------------
# AUTO — preserves today's behavior + WARN when public_url_base is missing
# ---------------------------------------------------------------------------


class TestAutoPolicy:
    def test_default_kwarg_is_auto(self):
        """Omitting ``asset_url_policy`` selects AUTO. Backward-compat."""
        backend = _S3ShapedBackend("b", public_url_base="https://cdn.example/")
        sink = ObjectStorageSink(backend)
        assert sink._asset_url_policy is URLPolicy.AUTO

    def test_auto_with_public_url_base_no_warn(self, caplog):
        """When ``public_url_base`` is configured, AUTO is happy-path silent."""
        backend = _S3ShapedBackend("b", public_url_base="https://cdn.example/")
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.AUTO)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warns, "should not warn when public_url_base is configured"

    def test_auto_without_public_url_base_warns_once(self, caplog):
        """The headline case: AUTO + no public_url_base emits a WARN."""
        backend = _S3ShapedBackend("private-bucket", public_url_base=None)
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.AUTO)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        msg = warns[0].getMessage()
        # Substring asserts — wording can shift, information content can't.
        assert "private-bucket" in msg
        assert "public_url_base" in msg
        assert "presigned_get_url" in msg
        assert "private buckets" in msg

    def test_auto_warns_once_per_process_per_bucket(self, caplog):
        """Module-level guard: same bucket, two sinks → one total WARN."""
        backend = _S3ShapedBackend("private-bucket", public_url_base=None)
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.AUTO)
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.AUTO)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1, "second construction must not re-warn"

    def test_auto_warns_per_distinct_bucket(self, caplog):
        """Different buckets warn independently — proves the tuple key
        is ``(bucket, policy)``, not just ``policy``."""
        backend_a = _S3ShapedBackend("bucket-a", public_url_base=None)
        backend_b = _S3ShapedBackend("bucket-b", public_url_base=None)
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(backend_a, asset_url_policy=URLPolicy.AUTO)
            ObjectStorageSink(backend_b, asset_url_policy=URLPolicy.AUTO)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 2
        msgs = " | ".join(r.getMessage() for r in warns)
        assert "bucket-a" in msgs
        assert "bucket-b" in msgs

    def test_auto_treats_empty_string_public_url_base_as_unset(self, caplog):
        """``public_url_base = ""`` is the same misconfiguration as
        ``None`` — both mean "I didn't wire a CDN." Symmetric with the
        PUBLIC validation guard so the two branches don't disagree."""
        backend = _S3ShapedBackend("misconfigured-bucket", public_url_base="")
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.AUTO)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "misconfigured-bucket" in warns[0].getMessage()

    def test_auto_skips_warn_for_backend_without_public_url_attr(self, caplog):
        """Backend-agnostic guard: a backend that doesn't declare
        ``public_url_base`` at all (e.g., a future non-S3 backend) must
        not trip the WARN. We can't know what's appropriate for it."""
        backend = _BackendWithoutPublicUrlAttr()
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.AUTO)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warns, "no WARN when the backend doesn't expose public_url_base"


# ---------------------------------------------------------------------------
# PUBLIC — requires public_url_base
# ---------------------------------------------------------------------------


class TestPublicPolicy:
    def test_public_with_public_url_base_constructs(self, caplog):
        """Happy path: PUBLIC + configured public_url_base → no error, no WARN."""
        backend = _S3ShapedBackend("b", public_url_base="https://cdn.example/")
        with caplog.at_level(logging.WARNING, logger="genblaze.storage.sink"):
            sink = ObjectStorageSink(backend, asset_url_policy=URLPolicy.PUBLIC)
        assert sink._asset_url_policy is URLPolicy.PUBLIC
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warns

    def test_public_without_public_url_base_raises(self):
        """PUBLIC + missing public_url_base → URLPolicyError at construction."""
        backend = _S3ShapedBackend("b", public_url_base=None)
        with pytest.raises(URLPolicyError) as excinfo:
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.PUBLIC)
        msg = str(excinfo.value)
        assert "public_url_base" in msg
        # Error message should point users at the alternative
        assert "URLPolicy.AUTO" in msg or "AUTO" in msg

    def test_public_rejects_empty_string_public_url_base(self):
        """``public_url_base = ""`` is treated as "not set" — same as
        ``None``. Otherwise empty-string misconfigurations would slip
        past PUBLIC validation and silently produce broken URLs at
        fetch time."""
        backend = _S3ShapedBackend("b", public_url_base="")
        with pytest.raises(URLPolicyError) as excinfo:
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.PUBLIC)
        # Error includes the actual value so empty-string misconfigs are
        # distinguishable from None at the message level.
        assert "''" in str(excinfo.value)

    def test_public_rejects_backend_without_public_url_base_attr(self):
        """A backend that doesn't expose `public_url_base` at all
        (non-S3-shaped) cannot satisfy PUBLIC mode. Sink must refuse
        loudly instead of silently constructing — the previous
        sentinel check passed PUBLIC through because `object()` is
        truthy. Error message names the backend type so the user
        knows which backend they need to swap or reconfigure."""
        backend = _BackendWithoutPublicUrlAttr()
        with pytest.raises(URLPolicyError) as excinfo:
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.PUBLIC)
        msg = str(excinfo.value)
        assert "public_url_base" in msg
        # Names the offending backend type for diagnosability.
        assert "_BackendWithoutPublicUrlAttr" in msg
        # No raw "<object object>" sentinel repr leakage.
        assert "<object" not in msg

    def test_public_does_not_warn_about_durable_url(self, caplog):
        """The PUBLIC error path raises before the AUTO WARN logic;
        the module-level WARN set stays clean."""
        from genblaze_core.storage.sink import _warned_durable_url

        backend = _S3ShapedBackend("b", public_url_base=None)
        with pytest.raises(URLPolicyError):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.PUBLIC)
        assert not _warned_durable_url


# ---------------------------------------------------------------------------
# PRESIGNED — rejected outright
# ---------------------------------------------------------------------------


class TestPresignedPolicy:
    def test_presigned_raises_at_construction(self):
        """PRESIGNED on the sink is a deliberate no — manifests must not
        carry SigV4 URLs that decay before the manifest does."""
        backend = _S3ShapedBackend("b", public_url_base=None)
        with pytest.raises(URLPolicyError) as excinfo:
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.PRESIGNED)
        msg = str(excinfo.value)
        # Error message names the alternative so the caller has a path.
        assert "presigned_get_url" in msg

    def test_presigned_raises_even_with_public_url_base(self):
        """Configuration of ``public_url_base`` doesn't unlock PRESIGNED —
        the rejection is unconditional."""
        backend = _S3ShapedBackend("b", public_url_base="https://cdn.example/")
        with pytest.raises(URLPolicyError):
            ObjectStorageSink(backend, asset_url_policy=URLPolicy.PRESIGNED)


# ---------------------------------------------------------------------------
# URLPolicy + URLPolicyError surface via genblaze_core.storage
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_url_policy_reexported_from_core_storage(self):
        """The canonical home is ``genblaze_core.storage.url_policy``;
        re-exported from ``genblaze_core.storage`` for ergonomics."""
        from genblaze_core.storage import (
            URLPolicy as UPolicy,
        )
        from genblaze_core.storage import (
            URLPolicyError as UPolicyError,
        )

        assert UPolicy is URLPolicy
        assert UPolicyError is URLPolicyError

    def test_url_policy_reexported_from_s3_for_back_compat(self):
        """``genblaze_s3.url_policy`` keeps the old import path working
        for callers from before the 0.3.1 relocation."""
        from genblaze_s3.url_policy import (
            URLPolicy as UPolicy,
        )
        from genblaze_s3.url_policy import (
            URLPolicyError as UPolicyError,
        )

        assert UPolicy is URLPolicy
        assert UPolicyError is URLPolicyError

    def test_url_policy_member_values_unchanged(self):
        """The enum's string values are part of the wire contract; a
        StrEnum's value identity must not silently shift."""
        assert URLPolicy.AUTO.value == "auto"
        assert URLPolicy.PUBLIC.value == "public"
        assert URLPolicy.PRESIGNED.value == "presigned"
