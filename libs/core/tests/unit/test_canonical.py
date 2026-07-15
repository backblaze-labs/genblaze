"""Tests for canonical JSON and hashing."""

import math

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
