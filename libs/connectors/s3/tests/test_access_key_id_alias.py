"""Tests for the ``access_key_id`` / ``secret_access_key`` kwarg aliases.

The S3 connector's README and several ecosystem examples use the
unprefixed form, but the historic kwarg name was ``aws_access_key_id``
(boto3 native). Phase 1A accepts either; passing both raises
``TypeError`` (no silent precedence). Closes bug #10 in the
storage-backend-hardening tranche.
"""

from __future__ import annotations

import pytest
from genblaze_s3 import S3StorageBackend


def test_short_form_kwargs_construct_backend() -> None:
    """The README quickstart's exact form must construct without error."""
    backend = S3StorageBackend(
        bucket="b",
        endpoint_url="https://s3.example.test",
        region="us-west-2",
        access_key_id="AKIA-test",
        secret_access_key="secret-test",
    )
    # Credentials persisted onto the instance; no silent drop.
    assert backend._aws_access_key_id == "AKIA-test"
    assert backend._aws_secret_access_key == "secret-test"  # noqa: S105 — test fixture


def test_aws_prefix_form_still_works() -> None:
    """Backwards-compat with every existing call site that passes the boto3 names."""
    backend = S3StorageBackend(
        bucket="b",
        endpoint_url="https://s3.example.test",
        region="us-west-2",
        aws_access_key_id="AKIA-test",
        aws_secret_access_key="secret-test",
    )
    assert backend._aws_access_key_id == "AKIA-test"
    assert backend._aws_secret_access_key == "secret-test"  # noqa: S105 — test fixture


def test_passing_both_access_key_names_raises() -> None:
    """Passing the same value under two names is a debugging trap — fail fast."""
    with pytest.raises(TypeError, match="aws_access_key_id.*access_key_id"):
        S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.example.test",
            region="us-west-2",
            aws_access_key_id="A",
            access_key_id="A",
        )


def test_passing_both_secret_key_names_raises() -> None:
    with pytest.raises(TypeError, match="aws_secret_access_key.*secret_access_key"):
        S3StorageBackend(
            bucket="b",
            endpoint_url="https://s3.example.test",
            region="us-west-2",
            aws_secret_access_key="S",
            secret_access_key="S",
        )


def test_mixed_aliases_one_each_is_allowed() -> None:
    """Caller may use the short form for one and the prefix form for the other."""
    backend = S3StorageBackend(
        bucket="b",
        endpoint_url="https://s3.example.test",
        region="us-west-2",
        aws_access_key_id="AKIA",
        secret_access_key="S",
    )
    assert backend._aws_access_key_id == "AKIA"
    assert backend._aws_secret_access_key == "S"  # noqa: S105 — test fixture


def test_neither_credential_passed_uses_boto3_defaults() -> None:
    """When neither alias is set, both credential fields stay ``None`` so boto3
    falls back to its env / instance-profile resolver. Pre-existing behavior."""
    backend = S3StorageBackend(
        bucket="b",
        endpoint_url="https://s3.example.test",
        region="us-west-2",
    )
    assert backend._aws_access_key_id is None
    assert backend._aws_secret_access_key is None
