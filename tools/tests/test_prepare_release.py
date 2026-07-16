"""Tests for tools/prepare_release.py.

Uses real, throwaway git repositories under ``tmp_path`` rather than mocked
git output. This script's entire value is in real git plumbing
(``describe --match``, ``diff --name-only``, ``show <tag>:<path>``) — mocking
it would hide exactly the bugs worth catching.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import prepare_release as pr  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture repo builder — a minimal-but-real genblaze-shaped monorepo.
# ---------------------------------------------------------------------------

_PYPROJECT_TEMPLATE = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "{version}"
description = "test package"
requires-python = ">=3.11"
dependencies = [
{deps}
]
{extras_block}
"""


def _deps_block(deps: list[str]) -> str:
    return "\n".join(f'    "{d}",' for d in deps)


def _write_pyproject(
    path: Path,
    name: str,
    version: str,
    deps: list[str],
    extras: dict[str, list[str]] | None = None,
) -> None:
    extras_block = ""
    if extras:
        lines = ["[project.optional-dependencies]"]
        for extra_name, extra_deps in extras.items():
            lines.append(f"{extra_name} = [")
            lines.extend(f'    "{d}",' for d in extra_deps)
            lines.append("]")
        extras_block = "\n".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _PYPROJECT_TEMPLATE.format(
            name=name, version=version, deps=_deps_block(deps), extras_block=extras_block
        ),
        encoding="utf-8",
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, "--no-gpg-sign")


def make_fixture_repo(
    tmp_path: Path,
    *,
    core_version: str = "0.1.0",
    cli_version: str = "0.1.0",
    meta_version: str = "0.1.0",
    spec_version: str = "0.1.0",
    connectors: dict[str, str] | None = None,
    connector_extras: dict[str, list[str]] | None = None,
    reserved_version: str = "0.9.0",
    baseline_tag: str = "v0.1.0",
) -> Path:
    """Build a real git repo mirroring the genblaze layout, commit it, and tag
    it ``baseline_tag`` (the "last release"). Returns the repo root; callers
    make further commits to simulate a wave in progress.

    Every git identity/signing setting is pinned per-repo (not ambient global
    config) so this works in a container with no global git config and
    doesn't hang if the host has commit signing turned on.
    """
    connectors = connectors if connectors is not None else {"openai": "0.1.0", "s3": "0.1.0"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "libs/core/genblaze_core/pipeline").mkdir(parents=True)
    (repo / "libs/core/genblaze_core/pipeline/pipeline.py").write_text(
        f'_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION = "{reserved_version}"\n',
        encoding="utf-8",
    )
    _write_pyproject(
        repo / "libs/core/pyproject.toml", "genblaze-core", core_version, ["pydantic>=2.0"]
    )

    _write_pyproject(
        repo / "cli/pyproject.toml",
        "genblaze-cli",
        cli_version,
        [f"genblaze-core>={core_version},<0.4", "click>=8.0"],
    )

    default_extras: dict[str, list[str]] = {}
    for name, version in connectors.items():
        pkg_name = f"genblaze-{name}"
        _write_pyproject(
            repo / f"libs/connectors/{name}/pyproject.toml",
            pkg_name,
            version,
            [f"genblaze-core>={core_version},<0.4"],
        )
        default_extras[name] = [f"{pkg_name}>={version},<0.4"]
    if connector_extras is not None:
        default_extras = connector_extras

    _write_pyproject(
        repo / "libs/meta/pyproject.toml",
        "genblaze",
        meta_version,
        [f"genblaze-core>={core_version},<0.4"],
        extras=default_extras,
    )

    (repo / "libs/spec").mkdir(parents=True, exist_ok=True)
    (repo / "libs/spec/package.json").write_text(
        f'{{\n  "name": "@genblaze/spec",\n  "version": "{spec_version}"\n}}\n',
        encoding="utf-8",
    )

    _commit(repo, "initial commit")
    _git(repo, "tag", "-a", baseline_tag, "-m", f"Release {baseline_tag}")
    return repo


def _touch_and_commit(repo: Path, rel_path: str, content: str, message: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _commit(repo, message)


# ---------------------------------------------------------------------------
# Pure unit tests — no git involved.
# ---------------------------------------------------------------------------


def test_bump_version_patch():
    assert pr.bump_version("0.3.6", "patch") == "0.3.7"


def test_bump_version_minor_resets_patch():
    assert pr.bump_version("0.3.6", "minor") == "0.4.0"


def test_bump_version_major_resets_minor_and_patch():
    assert pr.bump_version("1.2.3", "major") == "2.0.0"


def test_bump_version_rejects_malformed_input():
    with pytest.raises(pr.PrepareReleaseError):
        pr.bump_version("v1.2", "patch")


def test_set_pyproject_version_replaces_only_project_version():
    text = (
        '[build-system]\nrequires = ["hatchling"]\n\n'
        '[project]\nname = "genblaze-core"\nversion = "0.3.6"\ndescription = "x"\n\n'
        '[tool.pytest.ini_options]\nversion = "not-this-one"\n'
    )
    new_text = pr.set_pyproject_version(text, "0.3.7")
    assert 'version = "0.3.7"' in new_text
    assert 'version = "not-this-one"' in new_text  # untouched, outside [project]
    assert new_text.count('version = "0.3.7"') == 1


def test_set_pyproject_version_missing_raises():
    with pytest.raises(pr.PrepareReleaseError):
        pr.set_pyproject_version('[project]\nname = "x"\n', "1.0.0")


def test_set_package_json_version_replaces_first_occurrence():
    text = '{\n  "name": "@genblaze/spec",\n  "version": "0.4.0",\n  "other": "version"\n}\n'
    new_text = pr.set_package_json_version(text, "0.4.1")
    assert '"version": "0.4.1"' in new_text
    assert '"other": "version"' in new_text  # untouched


def test_rewrite_floors_updates_only_tracked_names_preserves_cap():
    text = 'dependencies = [\n    "genblaze-core>=0.3.6,<0.4",\n    "click>=8.0",\n]\n'
    new_text, changes = pr.rewrite_floors(text, {"genblaze-core": "core"}, {"core": "0.3.7"})
    assert '"genblaze-core>=0.3.7,<0.4",' in new_text
    assert '"click>=8.0",' in new_text  # untracked name untouched
    assert changes == [("genblaze-core", "genblaze-core>=0.3.6,<0.4", "genblaze-core>=0.3.7,<0.4")]


def test_rewrite_floors_no_op_when_floor_already_matches():
    text = 'dependencies = [\n    "genblaze-core>=0.3.7,<0.4",\n]\n'
    new_text, changes = pr.rewrite_floors(text, {"genblaze-core": "core"}, {"core": "0.3.7"})
    assert new_text == text
    assert changes == []


def test_rewrite_floors_updates_every_occurrence_across_extras():
    """A connector referenced in its own extra AND a bundle must get both
    lines updated (the real-world nvidia case: appears in 4 groups)."""
    text = (
        "[project.optional-dependencies]\n"
        'openai = [\n    "genblaze-openai>=0.1.0,<0.4",\n]\n'
        'all = [\n    "genblaze-openai>=0.1.0,<0.4",\n]\n'
    )
    new_text, changes = pr.rewrite_floors(text, {"genblaze-openai": "openai"}, {"openai": "0.1.1"})
    assert new_text.count('"genblaze-openai>=0.1.1,<0.4",') == 2
    assert len(changes) == 2


def test_map_changed_files_prefix_matching_does_not_false_positive():
    """`openai` and a hypothetical `openai-realtime` connector must not
    cross-match on a bare prefix (no trailing-slash boundary bug)."""
    packages = {
        "openai": pr.PackageInfo(
            "openai",
            "libs/connectors/openai",
            "libs/connectors/openai/pyproject.toml",
            "genblaze-openai",
            "0.1.0",
            "pyproject",
        ),
        "openai-realtime": pr.PackageInfo(
            "openai-realtime",
            "libs/connectors/openai-realtime",
            "libs/connectors/openai-realtime/pyproject.toml",
            "genblaze-openai-realtime",
            "0.1.0",
            "pyproject",
        ),
    }
    changed = pr.map_changed_files_to_keys(
        ["libs/connectors/openai-realtime/provider.py"], packages
    )
    assert changed == {"openai-realtime"}


def test_extract_reserved_core_version(tmp_path: Path):
    pipeline_dir = tmp_path / "libs/core/genblaze_core/pipeline"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "pipeline.py").write_text(
        '_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION = "0.4.0"\n', encoding="utf-8"
    )
    assert pr.extract_reserved_core_version(tmp_path) == "0.4.0"


def test_extract_reserved_core_version_missing_sentinel_raises(tmp_path: Path):
    pipeline_dir = tmp_path / "libs/core/genblaze_core/pipeline"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "pipeline.py").write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(pr.PrepareReleaseError):
        pr.extract_reserved_core_version(tmp_path)


def test_parse_overrides_set():
    assert pr.parse_overrides(["core=0.4.0"], "set") == {"core": "0.4.0"}


def test_parse_overrides_bump_rejects_bad_kind():
    with pytest.raises(pr.PrepareReleaseError):
        pr.parse_overrides(["core=sideways"], "bump")


def test_parse_overrides_rejects_missing_equals():
    with pytest.raises(pr.PrepareReleaseError):
        pr.parse_overrides(["core"], "set")


# ---------------------------------------------------------------------------
# Integration tests — real git fixture repos.
# ---------------------------------------------------------------------------


def test_default_patch_bump_for_changed_package(tmp_path: Path):
    repo = make_fixture_repo(tmp_path, core_version="0.3.6")
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: add a file")

    plan = pr.build_plan(repo)

    assert "core" in plan.changed_keys
    assert plan.version_changes["core"].old == "0.3.6"
    assert plan.version_changes["core"].new == "0.3.7"
    assert "code changed" in plan.version_changes["core"].reason


def test_floor_sync_forces_cli_and_meta_bump_when_only_core_changes(tmp_path: Path):
    repo = make_fixture_repo(
        tmp_path, core_version="0.3.6", cli_version="0.2.0", meta_version="0.5.0"
    )
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: bugfix")

    plan = pr.build_plan(repo)

    # cli's own files never changed...
    assert "cli" not in plan.changed_keys
    # ...but its core floor must move to match core's new version, forcing a bump.
    assert plan.version_changes["cli"].old == "0.2.0"
    assert plan.version_changes["cli"].new == "0.2.1"
    assert "dependency floor updated" in plan.version_changes["cli"].reason
    assert "genblaze-core" in plan.version_changes["cli"].reason

    assert plan.version_changes["meta"].old == "0.5.0"
    assert plan.version_changes["meta"].new == "0.5.1"

    cli_floor_change = [fc for fc in plan.floor_changes if fc.file_key == "cli"]
    assert len(cli_floor_change) == 1
    assert cli_floor_change[0].new_pin == "genblaze-core>=0.3.7,<0.4"


def test_connectors_only_wave_leaves_cli_untouched(tmp_path: Path):
    repo = make_fixture_repo(
        tmp_path,
        core_version="0.3.6",
        cli_version="0.2.0",
        meta_version="0.5.0",
        connectors={"openai": "0.1.0", "s3": "0.2.0"},
    )
    _touch_and_commit(
        repo, "libs/connectors/openai/genblaze_openai/newfile.py", "x = 1\n", "openai: bugfix"
    )

    plan = pr.build_plan(repo)

    assert plan.changed_keys == {"openai"}
    assert "cli" not in plan.version_changes  # regression guard: core never moved
    assert "core" not in plan.version_changes
    assert plan.version_changes["openai"].new == "0.1.1"
    # meta republishes because its openai floor now needs to move.
    assert plan.version_changes["meta"].new == "0.5.1"
    assert plan.version_changes["meta"].reason.startswith("dependency floor updated")
    assert "genblaze-openai" in plan.version_changes["meta"].reason


def test_reserved_version_refusal_via_explicit_minor_bump(tmp_path: Path):
    repo = make_fixture_repo(tmp_path, core_version="0.3.9", reserved_version="0.4.0")

    with pytest.raises(pr.ReservedVersionError, match="0.4.0"):
        pr.build_plan(repo, bump_overrides={"core": "minor"})


def test_reserved_version_refusal_via_explicit_set(tmp_path: Path):
    repo = make_fixture_repo(tmp_path, core_version="0.3.6", reserved_version="0.4.0")

    with pytest.raises(pr.ReservedVersionError, match="0.4.0"):
        pr.build_plan(repo, set_overrides={"core": "0.4.0"})


def test_reserved_version_guard_catches_hand_edited_core_on_check(tmp_path: Path):
    """The guard must fire even when the TOOL isn't the one deciding to bump
    core — e.g. a maintainer hand-edits core straight to the reserved
    version outside this script entirely. Regression for the blind spot
    where `--check` only validated tool-computed candidates."""
    repo = make_fixture_repo(tmp_path, core_version="0.3.9", reserved_version="0.4.0")
    # Hand-edit core to the reserved version directly (bypassing the tool)
    # and commit it — simulating a maintainer PR that already did this.
    text = (repo / "libs/core/pyproject.toml").read_text()
    (repo / "libs/core/pyproject.toml").write_text(
        pr.set_pyproject_version(text, "0.4.0"), encoding="utf-8"
    )
    _commit(repo, "core: hand-bump to 0.4.0 (oops)")

    with pytest.raises(pr.ReservedVersionError, match="0.4.0"):
        pr.build_plan(repo)


def test_reserved_version_exact_match_only_does_not_block_explicit_skip(tmp_path: Path):
    """Explicitly skipping past the reserved version via --set is a
    legitimate maintainer call, not the tool's business to block."""
    repo = make_fixture_repo(tmp_path, core_version="0.3.6", reserved_version="0.4.0")

    plan = pr.build_plan(repo, set_overrides={"core": "0.5.0"})

    assert plan.version_changes["core"].new == "0.5.0"


def test_released_versions_list_ordering(tmp_path: Path):
    repo = make_fixture_repo(
        tmp_path,
        core_version="0.3.6",
        cli_version="0.2.0",
        meta_version="0.5.0",
        connectors={"openai": "0.1.0", "zeta": "0.1.0"},
    )
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: bugfix")
    _touch_and_commit(
        repo, "libs/connectors/zeta/genblaze_zeta/newfile.py", "x = 1\n", "zeta: bugfix"
    )

    plan = pr.build_plan(repo)
    text = plan.released_versions_text
    order = [
        key
        for key in ("genblaze", "genblaze-core", "genblaze-cli", "genblaze-zeta")
        if key in text
    ]
    idx = {name: text.index(f"`{name}`") for name in order}
    assert idx["genblaze"] < idx["genblaze-core"] < idx["genblaze-cli"] < idx["genblaze-zeta"]
    assert "(umbrella)" in text


def test_spec_change_included_and_ts_types_warning(tmp_path: Path):
    repo = make_fixture_repo(tmp_path, spec_version="0.4.0")
    _touch_and_commit(repo, "libs/spec/schemas/asset.json", "{}\n", "spec: schema tweak")

    plan = pr.build_plan(repo)

    assert plan.version_changes["spec"].old == "0.4.0"
    assert plan.version_changes["spec"].new == "0.4.1"
    assert any("make ts-types" in w for w in plan.warnings)


def test_check_apply_idempotent_second_run_is_no_op(tmp_path: Path):
    repo = make_fixture_repo(
        tmp_path, core_version="0.3.6", cli_version="0.2.0", meta_version="0.5.0"
    )
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: bugfix")

    first_plan = pr.build_plan(repo)
    assert first_plan.prep_needed
    pr.apply_plan(repo, first_plan)

    # Re-run against the SAME commits (no new commit) — on-disk versions have
    # moved past what's published at the tag, so nothing should bump again.
    second_plan = pr.build_plan(repo)
    assert second_plan.prep_needed is False
    assert second_plan.version_changes == {}
    assert second_plan.floor_changes == []


def test_apply_writes_version_and_floor_to_disk(tmp_path: Path):
    repo = make_fixture_repo(
        tmp_path, core_version="0.3.6", cli_version="0.2.0", meta_version="0.5.0"
    )
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: bugfix")

    plan = pr.build_plan(repo)
    pr.apply_plan(repo, plan)

    core_text = (repo / "libs/core/pyproject.toml").read_text()
    cli_text = (repo / "cli/pyproject.toml").read_text()
    assert 'version = "0.3.7"' in core_text
    assert '"genblaze-core>=0.3.7,<0.4"' in cli_text
    assert 'version = "0.2.1"' in cli_text


def test_new_connector_with_no_meta_entry_warns_and_is_not_bumped(tmp_path: Path):
    repo = make_fixture_repo(tmp_path, connectors={"openai": "0.1.0"})
    # A brand-new connector added after the tag, never wired into meta's extras.
    _write_pyproject(
        repo / "libs/connectors/newconn/pyproject.toml",
        "genblaze-newconn",
        "0.1.0",
        ["genblaze-core>=0.3.0,<0.4"],
    )
    _commit(repo, "add newconn scaffold")

    plan = pr.build_plan(repo)

    assert "newconn" not in plan.version_changes  # brand new; nothing to bump
    assert any("genblaze-newconn" in w and "libs/meta/pyproject.toml" in w for w in plan.warnings)


def test_unknown_package_in_override_raises(tmp_path: Path):
    repo = make_fixture_repo(tmp_path)
    with pytest.raises(pr.PrepareReleaseError, match="unknown package"):
        pr.build_plan(repo, set_overrides={"not-a-real-package": "1.0.0"})


def test_no_tags_raises_clear_error(tmp_path: Path):
    repo = tmp_path / "no-tags-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _commit(repo, "initial commit, no tag")

    with pytest.raises(pr.PrepareReleaseError, match="no prior release tag"):
        pr.git_last_tag(repo)


# ---------------------------------------------------------------------------
# CLI (main()) — exit code contract.
# ---------------------------------------------------------------------------


def test_main_check_exits_zero_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo = make_fixture_repo(tmp_path)
    exit_code = pr.main(["--check", "--repo-root", str(repo)])
    assert exit_code == 0


def test_main_check_exits_one_when_prep_needed(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo = make_fixture_repo(tmp_path, core_version="0.3.6")
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: bugfix")
    exit_code = pr.main(["--check", "--repo-root", str(repo)])
    assert exit_code == 1


def test_main_apply_exits_zero_and_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo = make_fixture_repo(tmp_path, core_version="0.3.6")
    _touch_and_commit(repo, "libs/core/genblaze_core/newfile.py", "x = 1\n", "core: bugfix")
    exit_code = pr.main(["--apply", "--repo-root", str(repo)])
    assert exit_code == 0
    assert 'version = "0.3.7"' in (repo / "libs/core/pyproject.toml").read_text()


def test_main_returns_two_on_bad_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo = make_fixture_repo(tmp_path)
    exit_code = pr.main(["--check", "--set", "no-such-pkg=1.0.0", "--repo-root", str(repo)])
    assert exit_code == 2
