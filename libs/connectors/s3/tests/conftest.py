"""Shared pytest fixtures for the S3 connector test suite.

The ``mock_boto3`` fixture replaces ``boto3``/``botocore`` in
``sys.modules`` with mocks so unit tests don't make real network calls
or require AWS credentials. It also evicts any cached
``genblaze_s3.backend`` import so the backend's
``from botocore.exceptions import ClientError`` re-resolves to the
mocked ``_FakeClientError`` — without the eviction, an earlier test
that imported the real backend would leave the real ``ClientError``
bound in module scope, and any subsequent ``except ClientError`` would
miss the synthesized fakes.

Marked ``autouse=True`` so every test in the directory benefits without
having to opt in. Tests that need the underlying mock object as an
argument still receive it via the standard fixture-injection mechanism
(``def test_x(self, mock_boto3): ...``).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


class _FakeClientError(Exception):
    """Real exception subclass so backend code can ``except ClientError`` as usual."""

    def __init__(self, response, operation_name):
        super().__init__(operation_name)
        self.response = response
        self.operation_name = operation_name


# These are real Exception subclasses — the storage error classifier in
# ``genblaze_core.storage.errors.classify_botocore_error`` does
# ``isinstance(exc, ConnectTimeoutError)`` etc. against the imported
# botocore types. With ``MagicMock`` substitutions ``isinstance`` raises
# ``TypeError: isinstance() arg 2 must be a type, …``. Providing real
# subclasses keeps the classifier's lazy-import path working under the
# mocked-botocore test environment.
class _FakeConnectTimeoutError(Exception):
    def __init__(self, *, endpoint_url=""):
        super().__init__(f"connect timeout for {endpoint_url}")


class _FakeReadTimeoutError(Exception):
    def __init__(self, *, endpoint_url=""):
        super().__init__(f"read timeout for {endpoint_url}")


class _FakeBotoConnectionError(Exception):
    def __init__(self, *, error=""):
        super().__init__(f"connection error: {error}")


@pytest.fixture(autouse=True)
def mock_boto3():
    """Mock ``boto3`` / ``botocore`` for the duration of every test.

    Evicts any cached ``genblaze_s3`` / ``genblaze_s3.backend`` import
    so the next ``from genblaze_s3 ...`` triggers a fresh resolution
    with the mocks in place. Without this eviction, a test that
    constructed a real ``S3StorageBackend`` earlier in the same pytest
    session would have already bound the real ``ClientError`` into
    backend module scope, breaking ``except ClientError`` tests that
    raise the fake.
    """
    mock_mod = MagicMock()
    mock_botocore = MagicMock()
    # botocore.exceptions.* must be real exception classes — the backend
    # and the storage error classifier do ``except ClientError`` and
    # ``isinstance(exc, ConnectTimeoutError)`` checks.
    mock_botocore.exceptions.ClientError = _FakeClientError
    mock_botocore.exceptions.ConnectTimeoutError = _FakeConnectTimeoutError
    mock_botocore.exceptions.ReadTimeoutError = _FakeReadTimeoutError
    mock_botocore.exceptions.ConnectionError = _FakeBotoConnectionError

    modules = {
        "boto3": mock_mod,
        "boto3.s3": mock_mod.s3,
        "boto3.s3.transfer": mock_mod.s3.transfer,
        "botocore": mock_botocore,
        "botocore.config": mock_botocore.config,
        "botocore.exceptions": mock_botocore.exceptions,
    }

    # Force a fresh import of any backend module that closed over the
    # real ClientError. ``genblaze_s3/__init__.py`` re-exports the
    # backend, so both must go.
    for cached in ("genblaze_s3.backend", "genblaze_s3"):
        sys.modules.pop(cached, None)

    with patch.dict(sys.modules, modules):
        yield mock_mod

    # Post-test: evict again so the next test gets a clean re-import.
    for cached in ("genblaze_s3.backend", "genblaze_s3"):
        sys.modules.pop(cached, None)
