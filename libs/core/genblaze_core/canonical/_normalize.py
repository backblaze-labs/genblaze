"""Normalize Python values for deterministic JSON serialization."""

from __future__ import annotations

import math
import unicodedata
import uuid
from datetime import datetime
from enum import Enum
from typing import Any


def normalize(value: Any) -> Any:
    """Recursively normalize a value for canonical JSON output.

    - Dicts: sorted by key
    - Floats: rounded to 10 decimal places, NaN/Inf become null
    - Datetimes: ISO 8601 with Z suffix
    - Strings: Unicode NFC normalization
    - Enums: use .value
    - UUIDs: string representation
    - Pydantic models: converted via model_dump()
    - Unsupported types: raise TypeError (prevents silent non-determinism)
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 10)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError(
                f"normalize: naive datetime {value!r} — use utc_now() or attach tzinfo"
            )
        s = value.isoformat()
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if hasattr(value, "model_dump"):
        return normalize(value.model_dump())
    raise TypeError(
        f"normalize: unsupported type {type(value)!r} — add explicit handling "
        f"to preserve canonical JSON determinism"
    )
