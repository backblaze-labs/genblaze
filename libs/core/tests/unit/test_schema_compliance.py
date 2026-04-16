"""Tests that Python model output conforms to JSON schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step

jsonschema = pytest.importorskip("jsonschema")

SCHEMA_DIR = Path(__file__).resolve().parents[4] / "libs" / "spec" / "schemas" / "manifest" / "v1"

_SCHEMA_FILES = [
    "manifest.schema.json",
    "run.schema.json",
    "step.schema.json",
    "asset.schema.json",
]


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _make_resolver():
    """Create a resolver that can dereference $ref between schema files."""
    schemas = {_load_schema(f)["$id"]: _load_schema(f) for f in _SCHEMA_FILES}
    from jsonschema import RefResolver

    return RefResolver.from_schema(_load_schema("manifest.schema.json"), store=schemas)


def test_step_schema_compliance() -> None:
    """Step model output validates against step.schema.json."""
    step = Step(provider="test", model="test-model", prompt="hello")
    step_data = json.loads(step.model_dump_json())
    schema = _load_schema("step.schema.json")
    jsonschema.validate(step_data, schema)


def test_step_without_run_id_is_valid() -> None:
    """Step with run_id=None validates (nullable and optional)."""
    step = Step(provider="test", model="test-model")
    data = json.loads(step.model_dump_json())
    schema = _load_schema("step.schema.json")
    jsonschema.validate(data, schema)


def test_manifest_schema_compliance() -> None:
    """Full manifest output validates against manifest.schema.json."""
    step = Step(provider="test", model="test-model", prompt="a cat")
    run = Run(steps=[step])
    manifest = Manifest(run=run)
    manifest.compute_hash()

    manifest_data = json.loads(manifest.model_dump_json())
    resolver = _make_resolver()
    schema = _load_schema("manifest.schema.json")
    jsonschema.validate(manifest_data, schema, resolver=resolver)


def test_schema_version_matches_python() -> None:
    """Python SCHEMA_VERSION is in the JSON schema's allowed versions."""
    from genblaze_core.models.manifest import SCHEMA_VERSION

    schema = _load_schema("manifest.schema.json")
    allowed = schema["properties"]["schema_version"]["enum"]
    assert SCHEMA_VERSION in allowed
    # Current version should be the latest in the enum
    assert allowed[-1] == SCHEMA_VERSION
