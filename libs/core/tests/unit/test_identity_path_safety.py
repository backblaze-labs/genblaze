"""Identity fields must be safe single path components (GHSA-x99g-wcm2-r2v8).

``run_id``/``step_id``/``asset_id``/``tenant_id`` reach filesystem paths
(ParquetSink, connector ``output_dir`` writes) and object keys. They are
excluded from the canonical hash, so a tampered value in a shared manifest
still verifies; the models are the one chokepoint every path shares.
"""

from __future__ import annotations

import pytest
from genblaze_core._utils import validate_path_component
from genblaze_core.models.asset import Asset
from genblaze_core.models.manifest import Manifest, parse_manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from pydantic import ValidationError

_HOSTILE = ["../x", "..", ".", "a/b", "a\\b", "/abs", "C:x", "a\x00b", ""]
# Non-UUID ids already used by fixtures and documented examples stay valid.
_ALLOWED = ["r1", "intro clip", "v1.2", "-flag", "3f2b8c1e-0d5a-4c1e-9a8b-2f6d7e9c1a0b"]


@pytest.mark.parametrize("value", _HOSTILE)
def test_helper_rejects_hostile_components(value):
    with pytest.raises(ValueError, match="run_id"):
        validate_path_component(value, field="run_id")


@pytest.mark.parametrize("value", _ALLOWED)
def test_helper_accepts_single_components(value):
    assert validate_path_component(value, field="run_id") == value


@pytest.mark.parametrize("value", _HOSTILE)
@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda v: Run(run_id=v), "run_id"),
        (lambda v: Run(parent_run_id=v), "parent_run_id"),
        (lambda v: Run(tenant_id=v), "tenant_id"),
        (lambda v: Step(provider="p", model="m", step_id=v), "step_id"),
        (lambda v: Step(provider="p", model="m", run_id=v), "run_id"),
        (
            lambda v: Asset(url="https://example.com/a.png", media_type="image/png", asset_id=v),
            "asset_id",
        ),
    ],
)
def test_models_reject_hostile_identity_fields(factory, field, value):
    if field == "tenant_id" and value == "":
        pytest.skip("empty tenant_id normalizes to no tenant")
    if field == "tenant_id" and value == "C:x":
        pytest.skip("tenant_id allows ':' (see test_tenant_id_allows_colon)")
    with pytest.raises(ValidationError, match=field) as excinfo:
        factory(value)
    assert "single path component" in str(excinfo.value)


@pytest.mark.parametrize("value", _ALLOWED)
def test_models_accept_existing_id_styles(value):
    run = Run(run_id=value, parent_run_id=value, tenant_id=value)
    step = Step(provider="p", model="m", step_id=value, run_id=value)
    asset = Asset(url="https://example.com/a.png", media_type="image/png", asset_id=value)
    assert (run.run_id, step.step_id, asset.asset_id) == (value, value, value)


def test_tenant_id_allows_colon():
    """Tenants never reach a filesystem path raw (ParquetSink re-sanitizes
    partitions), so URN-style tenants such as ``org:acme`` keep working."""
    assert Run(tenant_id="org:acme").tenant_id == "org:acme"


def test_error_message_truncates_long_values():
    with pytest.raises(ValueError) as excinfo:
        validate_path_component("a/" * 10_000, field="run_id")
    assert len(str(excinfo.value)) < 300


def test_pipeline_rejects_bad_tenant_before_running_steps():
    """A bad tenant must fail at construction, not after paid provider calls."""
    from genblaze_core.pipeline.pipeline import Pipeline

    with pytest.raises(ValueError, match="Invalid tenant_id"):
        Pipeline("p", tenant_id="org/team")


def test_optional_identity_fields_accept_none():
    run = Run(parent_run_id=None, tenant_id=None)
    assert run.parent_run_id is None and run.tenant_id is None


def test_tampered_run_id_keeps_hash_but_fails_parse():
    """run_id is hash-excluded, so tampering can't be caught by verify; it
    has to be rejected at parse time, including tolerant loads."""
    run = Run(steps=[Step(provider="p", model="m")])
    manifest = Manifest.from_run(run)
    renamed = Manifest.from_run(run.model_copy(update={"run_id": "other"}))
    assert renamed.canonical_hash == manifest.canonical_hash  # verify can't see it

    data = manifest.model_dump(mode="json")
    data["run"]["run_id"] = "../../../tmp/pwned"

    with pytest.raises(ValidationError, match="Invalid run_id"):
        parse_manifest(data)


def test_tampered_nested_step_and_asset_ids_fail_parse():
    step = Step(provider="p", model="m")
    step.assets.append(Asset(url="https://example.com/a.png", media_type="image/png"))
    data = Manifest.from_run(Run(steps=[step])).model_dump(mode="json")
    data["run"]["steps"][0]["assets"][0]["asset_id"] = "/etc/x"

    with pytest.raises(ValidationError, match="Invalid asset_id"):
        parse_manifest(data)
