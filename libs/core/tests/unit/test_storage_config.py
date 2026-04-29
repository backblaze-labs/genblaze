"""Unit tests for ``genblaze_core.storage.config.StorageConfig``."""

from __future__ import annotations

import dataclasses

import pytest
from genblaze_core.storage.config import StorageConfig


def test_defaults_preserve_current_s3_backend_behavior() -> None:
    """Sanity: ``StorageConfig()`` matches the historic hardcoded values.

    These mirror the constants in ``libs/connectors/s3/genblaze_s3/backend.py``
    (lines 27-33). If a default changes here without a corresponding backend
    rev, the new ``StorageConfig`` no-op upgrade story breaks.
    """
    cfg = StorageConfig()
    assert cfg.max_pool_connections == 20
    assert cfg.connect_timeout_sec == 30.0
    assert cfg.read_timeout_sec == 300.0
    assert cfg.multipart_threshold == 16 * 1024 * 1024
    assert cfg.multipart_chunk_size == 16 * 1024 * 1024
    assert cfg.retries == 3
    assert cfg.user_agent_extra is None
    assert cfg.signing_addressing_style == "virtual"


def test_frozen_blocks_mutation() -> None:
    """Frozen dataclass — setting an attribute post-init must raise."""
    cfg = StorageConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_pool_connections = 50  # type: ignore[misc]


def test_validation_zero_pool() -> None:
    with pytest.raises(ValueError, match="max_pool_connections must be ≥ 1"):
        StorageConfig(max_pool_connections=0)


def test_validation_negative_timeout() -> None:
    with pytest.raises(ValueError, match="connect_timeout_sec must be > 0"):
        StorageConfig(connect_timeout_sec=-1.0)
    with pytest.raises(ValueError, match="read_timeout_sec must be > 0"):
        StorageConfig(read_timeout_sec=0)


def test_validation_negative_retries() -> None:
    # Zero retries is a valid choice (disable boto3-internal retry stack).
    StorageConfig(retries=0)
    with pytest.raises(ValueError, match="retries must be ≥ 0"):
        StorageConfig(retries=-1)


def test_validation_zero_multipart() -> None:
    with pytest.raises(ValueError, match="multipart_threshold must be ≥ 1"):
        StorageConfig(multipart_threshold=0)
    with pytest.raises(ValueError, match="multipart_chunk_size must be ≥ 1"):
        StorageConfig(multipart_chunk_size=0)


def test_user_agent_extra_optional() -> None:
    cfg = StorageConfig(user_agent_extra="my-app/1.0")
    assert cfg.user_agent_extra == "my-app/1.0"


def test_path_addressing_style_accepted() -> None:
    cfg = StorageConfig(signing_addressing_style="path")
    assert cfg.signing_addressing_style == "path"


def test_hashable_for_caching() -> None:
    """Frozen + slots → hashable. Backends may key clients on the config."""
    a = StorageConfig()
    b = StorageConfig()
    c = StorageConfig(max_pool_connections=50)
    assert hash(a) == hash(b)
    assert hash(a) != hash(c)
