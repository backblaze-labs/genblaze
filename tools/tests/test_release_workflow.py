"""Release policy regressions at the workflow's credential boundary."""

from pathlib import Path

import pytest
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
    assert "validation_only" not in inputs
    for job in workflow()["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@"):
                assert step["if"].startswith(
                    "needs.validate-version.outputs.upload_python == 'true' &&"
                )
                assert step["with"]["attestations"] is True
                assert "repository-url" not in step["with"]


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
        assert "inputs.dry_run == false" in step["if"]
    npm = jobs["publish-npm"]
    publish = next(s for s in npm["steps"] if s.get("name") == "Publish to npm")
    assert publish["if"] == "steps.npm_check.outputs.skip != 'true'"
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


@pytest.mark.parametrize("dry", ["true", "false"])
def test_npm_shell_passes_the_selected_publish_mode_to_npm(tmp_path, dry):
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
    env = dict(os.environ, DRY_RUN=dry, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=env, check=True, capture_output=True
    )
    calls = [json.loads(line) for line in (tmp_path / "npm-calls.jsonl").read_text().splitlines()]
    args = ["publish", "--access", "public"]
    if dry == "true":
        args.append("--dry-run")
    assert calls == [["--version"], [*args, "--provenance"]]


def test_read_step_propagates_policy_rejection_without_outputs(tmp_path):
    import os
    import subprocess
    import sys

    policy = tmp_path / "release-policy/tools/release_gate.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("raise SystemExit(17)\n")
    gate = next(
        s for s in workflow()["jobs"]["validate-version"]["steps"] if s.get("id") == "read"
    )
    output = tmp_path / "outputs"
    env = dict(
        os.environ,
        EVENT_NAME="release",
        GITHUB_OUTPUT=str(output),
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    result = subprocess.run(
        ["bash", "-c", gate["run"]], cwd=tmp_path, env=env, capture_output=True
    )
    assert result.returncode == 17
    assert not output.exists()


def test_mode_expressions_pin_production_and_preview_outcomes():
    jobs = workflow()["jobs"]
    preview = "github.event_name == 'workflow_dispatch' && inputs.dry_run"
    expected_guard = (
        "needs.validate-version.outputs.upload_python == 'true' && "
        "(github.event_name != 'workflow_dispatch' || inputs.dry_run == false)"
    )
    for name in ["publish-core", "publish-cli", "publish-connectors", "publish-meta"]:
        assert jobs[name]["environment"] == (
            "${{ (github.event_name != 'workflow_dispatch' || inputs.dry_run == false) "
            "&& 'pypi' || '' }}"
        )
        publish = next(
            s
            for s in jobs[name]["steps"]
            if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")
        )
        assert publish["if"] == expected_guard
    npm = jobs["publish-npm"]
    assert npm["environment"] == "${{ " + preview + " && 'npm-dryrun' || 'npm' }}"
    step = next(s for s in npm["steps"] if s.get("name") == "Publish to npm")
    assert step["env"]["DRY_RUN"] == "${{ " + preview + " && 'true' || 'false' }}"
    assert step["if"] == "steps.npm_check.outputs.skip != 'true'"
    gate = next(s for s in jobs["validate-version"]["steps"] if s.get("id") == "read")
    assert gate.get("if") is None
    assert gate.get("continue-on-error", False) is False
    assert gate["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert gate["env"]["INPUT_DRY_RUN"] == "${{ inputs.dry_run }}"
    assert gate["env"]["RELEASE_TAG"] == "${{ github.event.release.tag_name }}"
    assert gate["env"]["GH_TOKEN"] == "${{ github.token }}"  # noqa: S105 — expression, not a credential
    assert "INPUT_VALIDATION_ONLY" not in gate["env"]


def test_midwave_gate_commands_fail_and_block_every_publish_job(tmp_path):
    """Run the workflow's real gate commands with fixed published metadata."""
    import os
    import shutil
    import subprocess
    import sys
    import tomllib

    import check_pin_parity

    shutil.copytree(ROOT / ".changes", tmp_path / ".changes")
    for name in [".changie.yaml", "CHANGELOG.md", "Makefile"]:
        shutil.copy(ROOT / name, tmp_path)
    (tmp_path / "tools").mkdir()
    for name in ["changelog.py", "check_pin_parity.py"]:
        shutil.copy(ROOT / "tools" / name, tmp_path / "tools" / name)
    (tmp_path / ".changes/unreleased/_regression.yaml").write_text(
        "component: Internal\nkind: Fixed\nbody: Pending regression fixture\n"
        "time: 2026-11-18T00:00:00Z\n"
    )
    subprocess.run([sys.executable, "tools/changelog.py", "merge"], cwd=tmp_path, check=True)
    for folder in check_pin_parity.PACKAGES:
        original = tomllib.loads((ROOT / folder / "pyproject.toml").read_text())["project"]
        manifest = tmp_path / folder / "pyproject.toml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        deps = '["urllib3>=2"]' if original["name"] == "genblaze-core" else "[]"
        manifest.write_text(
            f'[project]\nname="{original["name"]}"\nversion="1.0.0"\ndependencies={deps}\n'
        )
    # Replace only the external PyPI transport, not pin comparison or gate exits.
    transport = tmp_path / "published-metadata"
    transport.mkdir()
    (transport / "sitecustomize.py").write_text(
        "import io,json,urllib.request\n"
        "def metadata(url, **kwargs):\n"
        "    assert url.startswith('https://pypi.org/pypi/') and url.endswith('/1.0.0/json')\n"
        "    deps=['urllib3>=1'] if '/genblaze-core/' in url else []\n"
        "    return io.BytesIO(json.dumps({'info': {'requires_dist': deps}}).encode())\n"
        "urllib.request.urlopen=metadata\n"
    )
    env = dict(
        os.environ,
        PYTHONPATH=str(transport),
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    jobs = workflow()["jobs"]
    outcomes = {"validate-version": "success", "release-smoke": "success"}
    for name, diagnostic in [
        ("changelog-gate", "unreleased fragments must be batched"),
        ("pin-parity", "1 drift"),
    ]:
        command = next(s["run"] for s in reversed(jobs[name]["steps"]) if "run" in s)
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", command],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode != 0
        assert diagnostic in result.stdout + result.stderr
        outcomes[name] = "failure"

    # GitHub's default job condition is success(): failed or skipped needs block
    # dependent jobs. Check the complete publish graph, including transitive needs.
    pending = set(jobs) - outcomes.keys()
    while pending:
        ready = {name for name in pending if set(jobs[name].get("needs", [])) <= outcomes.keys()}
        assert ready, "Release workflow has a dependency cycle"
        for name in ready:
            needs = jobs[name].get("needs", [])
            outcomes[name] = (
                "success" if all(outcomes[dep] == "success" for dep in needs) else "skipped"
            )
        pending -= ready
    publishers = {name for name in jobs if name.startswith("publish-")}
    assert publishers == {
        "publish-core",
        "publish-cli",
        "publish-connectors",
        "publish-meta",
        "publish-npm",
    }
    for name in publishers | {"build-npm-types"}:
        assert jobs[name].get("if") is None, "Publish jobs must retain default success gating"
        assert outcomes[name] == "skipped", name
