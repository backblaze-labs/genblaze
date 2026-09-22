"""Regression: presigned GET URLs against a Backblaze B2 endpoint must be
path-style + SigV4 — issue #246.

Browser clients hitting a private bucket's durable URL get a 401 (no
credentials); the fix is to hand out a presigned URL instead. But B2's
S3-compatible endpoint 403s on virtual-host-style presigns (bucket as a
subdomain) — it only accepts path-style requests (bucket in the URL path)
signed with AWS SigV4.

This module proves ``S3StorageBackend.presigned_get`` already produces that
shape with **no extra configuration** — boto3 defaults to path-style
addressing and SigV4 signing whenever a non-AWS ``endpoint_url`` is set,
which is exactly what ``for_backblaze()`` configures. That evidence is why
issue #246 was resolved as a documentation fix (see
docs/features/object-storage.md, "Browser access to private B2 buckets")
rather than a change to ``backend.py``'s client configuration.

Unlike the rest of this test suite, these tests need botocore's *real*
SigV4 signer to inspect the URL it produces — so this module overrides the
package-wide ``mock_boto3`` autouse fixture (see ``conftest.py``) with a
no-op. ``import boto3`` at module scope runs at collection time, before any
fixture (mocked or not) has executed for any test in the session, so it
binds the genuine ``boto3`` package into ``sys.modules`` — the mocked
tests' ``patch.dict`` restores that same real entry on teardown, so it
stays available here regardless of test order.
"""

from __future__ import annotations

from urllib.parse import urlparse

import boto3  # noqa: F401 — see module docstring: forces sys.modules["boto3"] to the real package
import pytest
from genblaze_s3.backend import S3StorageBackend


@pytest.fixture(autouse=True)
def mock_boto3():
    """Override the package-wide boto3 mock — these tests need the real signer."""
    yield None


def _make_real_backend(**overrides) -> S3StorageBackend:
    defaults: dict = {
        "bucket": "my-bucket",
        "endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "region": "us-west-004",
        "aws_access_key_id": "test-key-id",
        "aws_secret_access_key": "test-app-key",
    }
    defaults.update(overrides)
    backend = S3StorageBackend(**defaults)
    backend._region_verified = True  # skip the network HeadBucket preflight
    return backend


class TestBackblazePresignAddressing:
    def test_presigned_get_uses_path_style_not_virtual_host(self):
        """Bucket must be in the URL path, not the subdomain.

        Virtual-host style (``https://my-bucket.s3.<region>.backblazeb2.com/...``)
        403s against B2; path-style (``https://s3.<region>.backblazeb2.com/my-bucket/...``)
        is what B2 actually accepts.
        """
        backend = _make_real_backend()
        presigned = backend.presigned_get("some/key.png", expires_in=900)

        parsed = urlparse(presigned.url)
        assert parsed.netloc == "s3.us-west-004.backblazeb2.com"
        assert parsed.path == "/my-bucket/some/key.png"

    def test_presigned_get_uses_sigv4(self):
        """B2 requires AWS4-HMAC-SHA256 (SigV4); older SigV2 presigns fail."""
        backend = _make_real_backend()
        presigned = backend.presigned_get("some/key.png")

        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in presigned.url
        assert "X-Amz-Signature=" in presigned.url

    def test_presigned_get_url_matches_presigned_get(self):
        """The raw-str companion produces the identical path-style URL."""
        backend = _make_real_backend()
        url = backend.presigned_get_url("some/key.png")

        parsed = urlparse(url)
        assert parsed.netloc == "s3.us-west-004.backblazeb2.com"
        assert parsed.path == "/my-bucket/some/key.png"
