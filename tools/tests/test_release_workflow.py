"""Release policy regressions at the workflow's credential boundary."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow():
    data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    # PyYAML's YAML 1.1 resolver treats GitHub's unquoted "on" key as True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def test_existing_production_trigger_is_preserved_for_human_decision():
    triggers = workflow()["on"]
    assert triggers["release"]["types"] == ["published"]
    assert "push" not in triggers


def test_dispatch_defaults_to_validation_without_uploads():
    inputs = workflow()["on"]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True
    assert inputs["validation_only"]["default"] is True
    for job in workflow()["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@"):
                assert step["if"] == "needs.validate-version.outputs.upload_python == 'true'"
                assert step["with"]["attestations"] is True


def test_all_four_gates_precede_python_and_npm_builds():
    gates = {"validate-version", "changelog-gate", "release-smoke", "pin-parity"}
    for name in ["publish-core", "build-npm-types", "publish-npm"]:
        assert gates <= set(workflow()["jobs"][name]["needs"])


def test_downstream_checkouts_use_the_validated_commit():
    for name, job in workflow()["jobs"].items():
        for step in job.get("steps", []):
            if name != "validate-version" and str(step.get("uses", "")).startswith(
                "actions/checkout@"
            ):
                assert step["with"]["ref"] == "${{ needs.validate-version.outputs.source_sha }}"


def test_production_trust_gate_is_invoked():
    steps = workflow()["jobs"]["validate-version"]["steps"]
    assert any("tools/release_gate.py --plan" in step.get("run", "") for step in steps)
