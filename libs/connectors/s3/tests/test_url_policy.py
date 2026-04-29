"""Unit tests for ``genblaze_s3.url_policy``.

Phase 1A ships only the value object — wiring into ``S3StorageBackend.get_url``
lives in Phase 1D. These tests pin the public API contract.
"""

from __future__ import annotations

import pytest
from genblaze_core.exceptions import GenblazeError
from genblaze_s3 import URLPolicy, URLPolicyError


def test_members_are_lowercase_strenums() -> None:
    assert URLPolicy.AUTO == "auto"
    assert URLPolicy.PUBLIC == "public"
    assert URLPolicy.PRESIGNED == "presigned"


def test_members_round_trip_from_string() -> None:
    """StrEnum lookup by string value — useful for config/env-driven policy."""
    assert URLPolicy("auto") is URLPolicy.AUTO
    assert URLPolicy("public") is URLPolicy.PUBLIC
    assert URLPolicy("presigned") is URLPolicy.PRESIGNED


def test_unknown_string_raises() -> None:
    with pytest.raises(ValueError, match="'private'"):
        URLPolicy("private")


def test_url_policy_error_subclasses_genblaze_error() -> None:
    """Catch-all ``except GenblazeError`` must catch URLPolicyError."""
    err = URLPolicyError("conflict")
    assert isinstance(err, GenblazeError)
    assert str(err) == "conflict"
