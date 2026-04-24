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
import pydantic
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
from genblaze_core.observability.events import (
    AgentCompletedEvent,
    AgentIterationEvaluatedEvent,
    AgentIterationStartedEvent,
    PipelineCompletedEvent,
    PipelineFailedEvent,
    PipelineStartedEvent,
    StepCompletedEvent,
    StepFailedEvent,
    StepProgressEvent,
    StepRetriedEvent,
    StepStartedEvent,
    StreamEventAdapter,
)

SPEC_ROOT = Path(__file__).resolve().parents[3] / "spec" / "schemas"
SCHEMA_DIR = SPEC_ROOT / "manifest" / "v1"
EVENT_SCHEMA_DIR = SPEC_ROOT / "events" / "v1"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _load_event(name: str) -> dict:
    return json.loads((EVENT_SCHEMA_DIR / name).read_text())


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


# ---------------------------------------------------------------------------
# StreamEvent variants
#
# Parity rules mirror the manifest schemas: Pydantic fields must match schema
# properties 1:1 (excluding in-process-only fields like `step`/`result` which
# live on the model but are ``exclude=True`` and therefore never appear in
# ``model_dump``), every property needs a description, objects are closed,
# and a constructed event must round-trip through the schema validator.
# ---------------------------------------------------------------------------

EVENT_MODEL_SCHEMA_PAIRS = [
    ("PipelineStartedEvent", PipelineStartedEvent, "pipeline-started.schema.json"),
    ("PipelineCompletedEvent", PipelineCompletedEvent, "pipeline-completed.schema.json"),
    ("PipelineFailedEvent", PipelineFailedEvent, "pipeline-failed.schema.json"),
    ("StepStartedEvent", StepStartedEvent, "step-started.schema.json"),
    ("StepProgressEvent", StepProgressEvent, "step-progress.schema.json"),
    ("StepRetriedEvent", StepRetriedEvent, "step-retried.schema.json"),
    ("StepCompletedEvent", StepCompletedEvent, "step-completed.schema.json"),
    ("StepFailedEvent", StepFailedEvent, "step-failed.schema.json"),
    (
        "AgentIterationStartedEvent",
        AgentIterationStartedEvent,
        "agent-iteration-started.schema.json",
    ),
    (
        "AgentIterationEvaluatedEvent",
        AgentIterationEvaluatedEvent,
        "agent-iteration-evaluated.schema.json",
    ),
    ("AgentCompletedEvent", AgentCompletedEvent, "agent-completed.schema.json"),
]


def _serializable_fields(model) -> set[str]:
    """Names of model fields that actually appear in ``model_dump`` output.

    Fields marked ``exclude=True`` (in-process-only objects like ``step``
    and ``result``) are intentionally absent from the wire contract.
    """
    return {name for name, field in model.model_fields.items() if not field.exclude}


@pytest.mark.parametrize("name,model,schema_file", EVENT_MODEL_SCHEMA_PAIRS)
def test_event_field_set_matches_schema(name, model, schema_file):
    schema = _load_event(schema_file)
    py_fields = _serializable_fields(model)
    sch_fields = set(schema["properties"].keys())
    only_py = py_fields - sch_fields
    only_sch = sch_fields - py_fields
    assert not only_py, f"[{name}] Pydantic-only fields (update schema): {sorted(only_py)}"
    assert not only_sch, (
        f"[{name}] Schema-only fields (update model or remove): {sorted(only_sch)}"
    )


@pytest.mark.parametrize("name,model,schema_file", EVENT_MODEL_SCHEMA_PAIRS)
def test_event_schema_has_descriptions(name, model, schema_file):
    schema = _load_event(schema_file)
    missing = [p for p, spec in schema["properties"].items() if not spec.get("description")]
    truly_missing = [p for p in missing if not (model.model_fields[p].description or "").strip()]
    assert not truly_missing, (
        f"[{name}] fields without any description (schema or Pydantic): {truly_missing}"
    )


@pytest.mark.parametrize("name,model,schema_file", EVENT_MODEL_SCHEMA_PAIRS)
def test_event_schema_rejects_additional_properties(name, model, schema_file):
    schema = _load_event(schema_file)
    assert schema.get("additionalProperties") is False, (
        f"[{name}] schema must set additionalProperties=false"
    )


@pytest.mark.parametrize("name,model,schema_file", EVENT_MODEL_SCHEMA_PAIRS)
def test_event_type_discriminator_matches(name, model, schema_file):
    """The schema's `type` const must match the Pydantic Literal default."""
    schema = _load_event(schema_file)
    sch_type = schema["properties"]["type"]["const"]
    py_type = model.model_fields["type"].default
    assert py_type == sch_type, f"[{name}] discriminator drift: py={py_type!r} sch={sch_type!r}"


def _event_validator(schema_file: str):
    return jsonschema.Draft202012Validator(_load_event(schema_file))


def test_pipeline_started_roundtrip_validates():
    ev = PipelineStartedEvent(run_id="r1", total_steps=2, message="demo")
    _event_validator("pipeline-started.schema.json").validate(ev.to_dict())


def test_pipeline_completed_roundtrip_validates():
    ev = PipelineCompletedEvent(
        run_id="r1",
        run_status="completed",
        manifest_hash="a" * 64,
    )
    _event_validator("pipeline-completed.schema.json").validate(ev.to_dict())


def test_pipeline_failed_roundtrip_validates():
    ev = PipelineFailedEvent(
        run_id="r1",
        run_status="failed",
        message="boom",
    )
    _event_validator("pipeline-failed.schema.json").validate(ev.to_dict())


def test_step_started_roundtrip_validates():
    ev = StepStartedEvent(
        run_id="r1", step_id="s1", step_index=0, total_steps=1, provider="p", model="m"
    )
    _event_validator("step-started.schema.json").validate(ev.to_dict())


def test_step_progress_roundtrip_validates():
    ev = StepProgressEvent(
        step_id="s1",
        provider="p",
        model="m",
        progress_pct=0.5,
        preview_url="https://preview.test/f.jpg",
    )
    _event_validator("step-progress.schema.json").validate(ev.to_dict())


def test_step_completed_roundtrip_validates():
    ev = StepCompletedEvent(
        run_id="r1",
        step_id="s1",
        step_index=0,
        total_steps=1,
        provider="p",
        model="m",
        elapsed_sec=1.23,
        step_status="succeeded",
    )
    _event_validator("step-completed.schema.json").validate(ev.to_dict())


def test_step_failed_roundtrip_validates():
    ev = StepFailedEvent(
        run_id="r1",
        step_id="s1",
        step_index=0,
        total_steps=1,
        provider="p",
        model="m",
        elapsed_sec=1.0,
        error="safety filter",
        step_status="failed",
    )
    _event_validator("step-failed.schema.json").validate(ev.to_dict())


def test_agent_events_roundtrip_validate():
    started = AgentIterationStartedEvent(iteration=0, total=4, message="begin")
    _event_validator("agent-iteration-started.schema.json").validate(started.to_dict())

    evaluated = AgentIterationEvaluatedEvent(
        iteration=0, passed=False, score=0.42, feedback="try again"
    )
    _event_validator("agent-iteration-evaluated.schema.json").validate(evaluated.to_dict())

    completed = AgentCompletedEvent(passed=True, iterations=3, total_cost_usd=0.21)
    _event_validator("agent-completed.schema.json").validate(completed.to_dict())


_VARIANT_FIXTURES = [
    (
        "pipeline.started",
        PipelineStartedEvent,
        dict(run_id="r1", total_steps=2, message="demo"),
    ),
    (
        "pipeline.completed",
        PipelineCompletedEvent,
        dict(run_id="r1", run_status="completed", manifest_hash="a" * 64),
    ),
    (
        "pipeline.failed",
        PipelineFailedEvent,
        dict(run_id="r1", run_status="failed", message="boom"),
    ),
    (
        "step.started",
        StepStartedEvent,
        dict(run_id="r1", step_id="s1", step_index=0, total_steps=1, provider="p", model="m"),
    ),
    (
        "step.progress",
        StepProgressEvent,
        dict(step_id="s1", provider="p", model="m", progress_pct=0.5),
    ),
    (
        "step.retried",
        StepRetriedEvent,
        dict(
            step_id="s1",
            provider="p",
            model="m",
            phase="poll",
            attempt=1,
            max_attempts=6,
            delay_sec=1.5,
            error_code="server_error",
            error="503",
        ),
    ),
    (
        "step.completed",
        StepCompletedEvent,
        dict(
            run_id="r1",
            step_id="s1",
            step_index=0,
            total_steps=1,
            provider="p",
            model="m",
            elapsed_sec=1.0,
            step_status="succeeded",
        ),
    ),
    (
        "step.failed",
        StepFailedEvent,
        dict(
            run_id="r1",
            step_id="s1",
            step_index=0,
            total_steps=1,
            provider="p",
            model="m",
            elapsed_sec=1.0,
            error="boom",
            step_status="failed",
        ),
    ),
    (
        "agent.iteration.started",
        AgentIterationStartedEvent,
        dict(iteration=0, total=4, message="begin"),
    ),
    (
        "agent.iteration.evaluated",
        AgentIterationEvaluatedEvent,
        dict(iteration=0, passed=False, score=0.42, feedback="try again"),
    ),
    (
        "agent.completed",
        AgentCompletedEvent,
        dict(passed=True, iterations=3, total_cost_usd=0.21),
    ),
]


@pytest.mark.parametrize("type_tag,variant,kwargs", _VARIANT_FIXTURES)
def test_stream_event_discriminator_roundtrip(type_tag, variant, kwargs):
    """Every variant round-trips through ``StreamEventAdapter`` to its own class.

    Exercises both directions of the wire contract: serializing to JSON via
    ``to_dict`` then re-parsing must recover the same variant with the same
    key fields. Guards against silent discriminator drift between the Pydantic
    ``Literal[...]`` and the schema ``const``.
    """
    original = variant(**kwargs)
    assert original.type == type_tag
    reparsed = StreamEventAdapter.validate_python(original.to_dict())
    assert isinstance(reparsed, variant)
    assert reparsed.type == type_tag


def test_stream_event_adapter_rejects_missing_type():
    """Missing discriminator → validation error, not a silent base instance."""
    with pytest.raises(pydantic.ValidationError):
        StreamEventAdapter.validate_python({"timestamp": "2026-04-24T00:00:00Z"})


def test_stream_event_adapter_rejects_unknown_type():
    """Unknown discriminator value → validation error."""
    with pytest.raises(pydantic.ValidationError):
        StreamEventAdapter.validate_python(
            {"type": "pipeline.nope", "timestamp": "2026-04-24T00:00:00Z"}
        )


def test_stream_event_adapter_rejects_missing_required_field():
    """Correct discriminator but missing a variant-required field fails."""
    # step.started requires run_id, step_id, step_index, total_steps, provider, model
    with pytest.raises(pydantic.ValidationError):
        StreamEventAdapter.validate_python({"type": "step.started", "run_id": "r1"})


def test_stream_event_schema_covers_every_variant():
    """stream-event.schema.json's oneOf must reference every variant file."""
    parent = _load_event("stream-event.schema.json")
    expected = {
        "pipeline-started.schema.json",
        "pipeline-completed.schema.json",
        "pipeline-failed.schema.json",
        "step-started.schema.json",
        "step-progress.schema.json",
        "step-retried.schema.json",
        "step-completed.schema.json",
        "step-failed.schema.json",
        "agent-iteration-started.schema.json",
        "agent-iteration-evaluated.schema.json",
        "agent-completed.schema.json",
    }
    refs = {entry["$ref"] for entry in parent["oneOf"]}
    assert refs == expected, f"stream-event.oneOf drift — refs={sorted(refs)}"

    # And the discriminator mapping must agree with the oneOf set.
    disc_values = set(parent["discriminator"]["mapping"].values())
    assert disc_values == expected, f"discriminator mapping drift — values={sorted(disc_values)}"
