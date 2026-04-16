"""Canonical JSON serialization and SHA-256 hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from genblaze_core.canonical._normalize import normalize


def canonical_json(data: Any) -> str:
    """Produce a deterministic JSON string from the given data.

    Keys are sorted, floats normalized, no trailing whitespace.
    """
    normalized = normalize(data)
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_hash(data: Any) -> str:
    """Compute SHA-256 hex digest of the canonical JSON representation."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
