"""Spec/Pydantic conformance tests.

These tests guard the contract boundary between Pydantic runtime models
(``libs/core/genblaze_core/models/``) and the language-neutral JSON
Schemas (``libs/spec/schemas/manifest/v1/``) that drive TypeScript
codegen.

If a model field is added/removed, the schema MUST be updated in the
same PR or CI will fail here. This is intentional: the schemas are the
public wire contract, and downstream `@genblaze/spec` TS consumers
depend on parity with Pydantic serialization.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import (
    Modality,
    PromptVisibility,
    ProviderErrorCode,
    RunStatus,
    StepStatus,
    StepType,
)
from genblaze_core.models.manifest import SCHEMA_VERSION, Manifest
from genblaze_core.models.policy import EmbedPolicy
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "spec" / "schemas" / "manifest" / "v1"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


MODEL_SCHEMA_PAIRS = [
    ("Asset", Asset, "asset.schema.json"),
    ("Run", Run, "run.schema.json"),
    ("Step", Step, "step.schema.json"),
    ("Manifest", Manifest, "manifest.schema.json"),
    ("EmbedPolicy", EmbedPolicy, "policy.schema.json"),
]


@pytest.mark.parametrize("name,model,schema_file", MODEL_SCHEMA_PAIRS)
def test_field_set_matches_schema(name, model, schema_file):
    """Pydantic fields and schema properties must be the same set.

    Catches both directions of drift:
    - A new Pydantic field with no schema entry (TS consumers lack the field).
    - A phantom schema property with no Pydantic backing (TS consumers
      expect a field that never appears on the wire).
    """
    schema = _load(schema_file)
    py_fields = set(model.model_fields.keys())
    sch_fields = set(schema["properties"].keys())
    only_py = py_fields - sch_fields
    only_sch = sch_fields - py_fields
    assert not only_py, f"[{name}] Pydantic-only fields (update schema): {sorted(only_py)}"
    assert not only_sch, (
        f"[{name}] Schema-only fields (update model or remove): {sorted(only_sch)}"
    )


@pytest.mark.parametrize("name,model,schema_file", MODEL_SCHEMA_PAIRS)
def test_schema_has_descriptions(name, model, schema_file):
    """Every schema property must carry a description so TS JSDoc isn't empty."""
    schema = _load(schema_file)
    missing = [p for p, spec in schema["properties"].items() if not spec.get("description")]
    # Allow description to live on the Pydantic field when schema omits it.
    truly_missing = [p for p in missing if not (model.model_fields[p].description or "").strip()]
    assert not truly_missing, (
        f"[{name}] fields without any description (schema or Pydantic): {truly_missing}"
    )


@pytest.mark.parametrize("name,model,schema_file", MODEL_SCHEMA_PAIRS)
def test_schema_rejects_additional_properties(name, model, schema_file):
    """Wire contract must be closed — prevents phantom field drift at runtime."""
    schema = _load(schema_file)
    assert schema.get("additionalProperties") is False, (
        f"[{name}] schema must set additionalProperties=false"
    )


ENUM_CHECKS = [
    ("Step.status", StepStatus, "step.schema.json", ("status",)),
    ("Step.step_type", StepType, "step.schema.json", ("step_type",)),
    ("Step.modality", Modality, "step.schema.json", ("modality",)),
    ("Step.prompt_visibility", PromptVisibility, "step.schema.json", ("prompt_visibility",)),
    ("Run.status", RunStatus, "run.schema.json", ("status",)),
    (
        "EmbedPolicy.prompt_visibility",
        PromptVisibility,
        "policy.schema.json",
        ("prompt_visibility",),
    ),
]


@pytest.mark.parametrize("label,enum_cls,schema_file,path", ENUM_CHECKS)
def test_enum_values_match(label, enum_cls, schema_file, path):
    """Enum value sets must agree so TS literal unions describe reality."""
    schema = _load(schema_file)
    node = schema["properties"]
    for part in path:
        node = node[part]
    py_values = {e.value for e in enum_cls}
    sch_values = {v for v in node["enum"] if v is not None}
    assert py_values == sch_values, (
        f"[{label}] drift: py={sorted(py_values)} schema={sorted(sch_values)}"
    )


def test_provider_error_code_enum_matches():
    """Step.error_code enum is nullable — filter None before comparing."""
    schema = _load("step.schema.json")
    sch = set(schema["properties"]["error_code"]["enum"]) - {None}
    py = {e.value for e in ProviderErrorCode}
    assert py == sch, f"drift: py={sorted(py)} schema={sorted(sch)}"


def test_manifest_schema_version_is_listed():
    """Current SCHEMA_VERSION constant must be in manifest.schema_version enum."""
    schema = _load("manifest.schema.json")
    assert SCHEMA_VERSION in schema["properties"]["schema_version"]["enum"], (
        f"SCHEMA_VERSION={SCHEMA_VERSION!r} missing from manifest.schema.json enum"
    )


def _schema_store() -> dict:
    """Resolve cross-file $refs against on-disk schemas for validation."""
    return {
        json.loads(p.read_text())["$id"]: json.loads(p.read_text())
        for p in SCHEMA_DIR.glob("*.schema.json")
    }


def _validator_for(schema_file: str):
    schema = _load(schema_file)
    resolver = jsonschema.RefResolver(
        base_uri=schema["$id"], referrer=schema, store=_schema_store()
    )
    return jsonschema.Draft202012Validator(schema, resolver=resolver)


def test_asset_roundtrip_validates():
    asset = Asset(url="https://example.com/out.png", media_type="image/png")
    _validator_for("asset.schema.json").validate(asset.model_dump(mode="json"))


def test_step_roundtrip_validates():
    step = Step(provider="mock", model="mock-v1", prompt="hello")
    _validator_for("step.schema.json").validate(step.model_dump(mode="json"))


def test_run_roundtrip_validates():
    step = Step(provider="mock", model="mock-v1")
    run = Run(steps=[step])
    _validator_for("run.schema.json").validate(run.model_dump(mode="json"))


def test_manifest_roundtrip_validates():
    step = Step(provider="mock", model="mock-v1")
    run = Run(steps=[step])
    manifest = Manifest.from_run(run)
    _validator_for("manifest.schema.json").validate(manifest.model_dump(mode="json"))


def test_embed_policy_roundtrip_validates():
    policy = EmbedPolicy()
    _validator_for("policy.schema.json").validate(policy.model_dump(mode="json"))
