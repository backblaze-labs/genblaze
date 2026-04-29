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
    # botocore.exceptions.ClientError must be a real exception class —
    # the backend does ``except ClientError``, which requires a real type.
    mock_botocore.exceptions.ClientError = _FakeClientError

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
