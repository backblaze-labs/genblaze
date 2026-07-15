"""Tests for canonical JSON and hashing."""

import math
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum

import pytest
from genblaze_core.canonical._normalize import normalize
from genblaze_core.canonical.json import canonical_hash, canonical_json
from genblaze_core.exceptions import ManifestError


def test_sorted_keys():
    result = canonical_json({"b": 1, "a": 2})
    assert result == '{"a":2,"b":1}'


def test_float_normalization():
    assert normalize(1.00000000001) == 1.0
    assert normalize(math.nan) is None
    assert normalize(math.inf) is None
    assert normalize(-math.inf) is None


# --- Issue #50: cover the normalization branches left untested — these feed
# canonical_hash()/to_canonical_json(), so a regression here silently changes
# (or fails to change) a manifest's provenance hash. ---


def test_enum_normalization():
    """Enum values normalize to .value, not their name or repr."""

    class Color(Enum):
        RED = "red"

    assert normalize(Color.RED) == "red"
    assert canonical_json({"color": Color.RED}) == '{"color":"red"}'


def test_naive_datetime_raises_type_error():
    """A timezone-naive datetime is rejected outright — canonical hashing
    requires an explicit offset so the same instant always normalizes
    identically regardless of the caller's local timezone."""
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(TypeError, match="naive datetime"):
        normalize(naive)


def test_aware_utc_datetime_serializes_with_z_suffix():
    """A +00:00 offset canonicalizes to the RFC 3339 'Z' shorthand."""
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert normalize(dt) == "2026-01-01T12:00:00Z"


def test_aware_non_utc_datetime_round_trips_stably():
    """A non-UTC offset is preserved (not forced to Z or shifted to UTC) and
    is stable across repeated normalization calls — the hash must not depend
    on incidental timezone-conversion behavior."""
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    result = normalize(dt)
    assert result == dt.isoformat()
    assert not result.endswith("Z")
    assert normalize(dt) == result


def test_nested_sort():
    data = {"z": {"b": 1, "a": 2}, "a": [3, 2, 1]}
    result = canonical_json(data)
    assert '"a":[3,2,1]' in result
    assert '"z":{"a":2,"b":1}' in result


def test_hash_deterministic():
    data = {"prompt": "a cat", "model": "flux"}
    h1 = canonical_hash(data)
    h2 = canonical_hash(data)
    assert h1 == h2
    assert len(h1) == 64


def test_hash_changes_with_data():
    h1 = canonical_hash({"prompt": "a cat"})
    h2 = canonical_hash({"prompt": "a dog"})
    assert h1 != h2


def test_v1_3_manifest_verifies_with_ids_in_hash():
    """Old v1.3 manifests (IDs included in hash) still verify correctly."""
    from genblaze_core.models.manifest import Manifest, parse_manifest
    from genblaze_core.models.run import Run
    from genblaze_core.models.step import Step

    s = Step(provider="test", model="m", prompt="hello")
    r = Run(steps=[s])
    m = Manifest(run=r, schema_version="1.3")
    m.compute_hash()
    assert m.verify()

    # Serialize, parse, and verify again
    data = m.model_dump(mode="python")
    parsed = parse_manifest(data)
    assert parsed.verify()


def test_v1_4_manifest_deterministic_across_runs():
    """v1.4 manifests exclude IDs, so identical inputs → identical hash."""
    from genblaze_core.models.manifest import Manifest
    from genblaze_core.models.run import Run
    from genblaze_core.models.step import Step

    m1 = Manifest.from_run(Run(steps=[Step(provider="p", model="m", prompt="x")]))
    m2 = Manifest.from_run(Run(steps=[Step(provider="p", model="m", prompt="x")]))
    assert m1.canonical_hash == m2.canonical_hash


def _deeply_nested_dict(depth: int) -> dict:
    nested: dict = {}
    cursor = nested
    for _ in range(depth):
        cursor["a"] = {}
        cursor = cursor["a"]
    return nested


class TestNormalizeDepthGuard:
    """Regression for #81: step.params is free-form user input hashed into
    every manifest and cache key; pathological nesting must fail with a
    typed, catchable error rather than an uncaught RecursionError."""

    def test_deeply_nested_dict_raises_manifest_error(self):
        with pytest.raises(ManifestError, match="max depth"):
            canonical_hash(_deeply_nested_dict(1000))

    def test_deeply_nested_list_raises_manifest_error(self):
        nested: list = []
        cursor = nested
        for _ in range(1000):
            child: list = []
            cursor.append(child)
            cursor = child
        with pytest.raises(ManifestError, match="max depth"):
            canonical_hash(nested)

    def test_shallow_nesting_still_normalizes_fine(self):
        """The depth guard must not reject realistic, shallow-nested params."""
        data = {"a": {"b": {"c": [1, 2, {"d": "e"}]}}}
        assert normalize(data) == data

    def test_deeply_nested_step_params_raises_manifest_error(self):
        """End-to-end: hashing a manifest or cache key built from a
        pathologically nested step.params must not crash the run/request."""
        from genblaze_core.models.step import Step
        from genblaze_core.pipeline.cache import step_cache_key

        step = Step(provider="p", model="m", params=_deeply_nested_dict(1000))
        with pytest.raises(ManifestError, match="max depth"):
            step_cache_key(step)
