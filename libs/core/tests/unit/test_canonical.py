"""Tests for canonical JSON and hashing."""

import math

from genblaze_core.canonical._normalize import normalize
from genblaze_core.canonical.json import canonical_hash, canonical_json


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
