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
                assert step["if"].startswith(
                    "needs.validate-version.outputs.upload_python == 'true' &&"
                )
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
    command = next(step["run"] for step in steps if step.get("id") == "read")
    assert 'python "$policy_dir/tools/release_gate.py" --plan --repo-root candidate' in command
    assert "policy_dir=release-policy" in command


def test_production_policy_is_checked_out_independently_of_candidate():
    steps = workflow()["jobs"]["validate-version"]["steps"]
    trust = [s for s in steps if s.get("with", {}).get("path") == "release-policy"]
    assert len(trust) == 1
    assert trust[0]["with"]["ref"] == "main"
    command = next(s["run"] for s in steps if s.get("id") == "read")
    assert "policy_dir=release-policy" in command
    assert "--repo-root candidate" in command


def test_upload_guards_do_not_trust_candidate_mode_outputs():
    jobs = workflow()["jobs"]
    publishers = [
        s
        for j in jobs.values()
        for s in j.get("steps", [])
        if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")
    ]
    assert len(publishers) == 4
    for step in publishers:
        assert "inputs.validation_only == false" in step["if"]
    npm = jobs["publish-npm"]
    publish = next(s for s in npm["steps"] if s.get("name") == "Publish to npm")
    assert "inputs.dry_run" in publish["if"]
    assert "npm publish --access public --dry-run --provenance" in publish["run"]
    assert "inputs.dry_run" in publish["env"]["DRY_RUN"]
    assert "outputs.dry_run" not in npm["environment"]


def test_policy_selection_runs_trusted_main_code_for_production(tmp_path):
    import json
    import os
    import subprocess
    import sys

    for folder in ["release-policy", "candidate"]:
        path = tmp_path / folder / "tools/release_gate.py"
        path.parent.mkdir(parents=True)
        path.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            'Path("chosen.json").write_text(json.dumps({"source": '
            + repr(folder)
            + ', "args": sys.argv[1:]}))\n'
        )
    command = next(
        s["run"] for s in workflow()["jobs"]["validate-version"]["steps"] if s.get("id") == "read"
    )
    for dry, expected in [("false", "release-policy"), ("true", "candidate")]:
        env = dict(
            os.environ,
            EVENT_NAME="workflow_dispatch",
            INPUT_DRY_RUN=dry,
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        )
        subprocess.run(
            ["bash", "-c", command], cwd=tmp_path, env=env, check=True, capture_output=True
        )
        chosen = json.loads((tmp_path / "chosen.json").read_text())
        assert chosen == {"source": expected, "args": ["--plan", "--repo-root", "candidate"]}


def test_npm_dispatch_executes_dry_run_without_registry_upload(tmp_path):
    import json
    import os
    import subprocess
    import sys

    npm = tmp_path / "npm"
    npm.write_text(
        "#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
        'with Path("npm-calls.jsonl").open("a") as f:\n'
        '    f.write(json.dumps(sys.argv[1:])+"\\n")\n'
    )
    npm.chmod(0o755)
    step = next(
        s for s in workflow()["jobs"]["publish-npm"]["steps"] if s.get("name") == "Publish to npm"
    )
    env = dict(os.environ, DRY_RUN="true", PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=env, check=True, capture_output=True
    )
    calls = [json.loads(line) for line in (tmp_path / "npm-calls.jsonl").read_text().splitlines()]
    assert calls == [["--version"], ["publish", "--access", "public", "--dry-run", "--provenance"]]
