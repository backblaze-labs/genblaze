"""Production trust checks use real Git/SSH signatures, never mocked verification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_gate import ReleaseGateError, check_milestone, release_mode, verify_tag, write_plan


def run(repo, *args):
    return subprocess.run(args, cwd=repo, text=True, capture_output=True, check=True)


@pytest.fixture
def signed_repo(tmp_path):
    run(tmp_path, "git", "init", "-b", "main")
    run(tmp_path, "git", "config", "user.name", "Release Test")
    run(tmp_path, "git", "config", "user.email", "release@example.invalid")
    key = tmp_path / "test-key"
    run(tmp_path, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
    run(tmp_path, "git", "config", "gpg.format", "ssh")
    run(tmp_path, "git", "config", "user.signingkey", str(key))
    trust = tmp_path / ".github/release-signers"
    trust.parent.mkdir()
    trust.write_text("release@example.invalid " + key.with_suffix(".pub").read_text())
    run(tmp_path, "git", "add", ".github/release-signers")
    run(tmp_path, "git", "commit", "-m", "initial trust")
    return tmp_path


def test_trusted_signed_tag_passes(signed_repo):
    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    verify_tag(signed_repo, "v1.2.3", "main")


@pytest.mark.parametrize("annotated", [True, False])
def test_unsigned_and_lightweight_tags_fail(signed_repo, annotated):
    args = ["git", "tag"]
    if annotated:
        args += ["-a", "-m", "unsigned"]
    run(signed_repo, *args, "v1.2.3")
    with pytest.raises(ReleaseGateError):
        verify_tag(signed_repo, "v1.2.3", "main")


def test_untrusted_signer_fails(signed_repo):
    other = signed_repo / "other-key"
    run(signed_repo, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other))
    run(
        signed_repo,
        "git",
        "-c",
        f"user.signingkey={other}",
        "tag",
        "-s",
        "v1.2.3",
        "-m",
        "release",
    )
    with pytest.raises(ReleaseGateError):
        verify_tag(signed_repo, "v1.2.3", "main")


def test_tag_cannot_authorize_itself_or_release_off_main(signed_repo):
    run(signed_repo, "git", "switch", "-c", "unreviewed")
    (signed_repo / ".github/release-signers").write_text("attacker@example.invalid invalid-key\n")
    run(signed_repo, "git", "add", ".github/release-signers")
    run(signed_repo, "git", "commit", "-m", "unreviewed trust")
    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    with pytest.raises(ReleaseGateError, match="main"):
        verify_tag(signed_repo, "v1.2.3", "main")


def test_worktree_cannot_replace_trusted_key_list(signed_repo):
    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    (signed_repo / ".github/release-signers").write_text("invalid worktree key\n")
    verify_tag(signed_repo, "v1.2.3", "main")


@pytest.mark.parametrize(
    "milestones",
    [
        [],
        [{"title": "v1.2.3", "state": "open", "open_issues": 0}],
        [{"title": "v1.2.3", "state": "closed", "open_issues": 1}],
    ],
)
def test_missing_or_unfinished_milestone_fails(milestones):
    with pytest.raises(ReleaseGateError):
        check_milestone(milestones, "1.2.3")


def test_closed_empty_wave_milestone_passes():
    check_milestone([{"title": "v1.2.3", "state": "closed", "open_issues": 0}], "1.2.3")


@pytest.mark.parametrize("value", ["", "false; echo injected", "1", "TRUE"])
def test_dispatch_mode_fails_closed_on_invalid_boolean(value):
    with pytest.raises(ReleaseGateError):
        release_mode("workflow_dispatch", value, "true")


def test_release_event_cannot_be_made_a_preview():
    assert release_mode("release", "true", "true") == (False, False)


def test_default_dispatch_is_non_publishing():
    assert release_mode("workflow_dispatch", "true", "true") == (True, True)


def test_explicit_testpypi_upload_remains_available():
    assert release_mode("workflow_dispatch", "true", "false") == (True, False)


def test_validation_plan_never_enables_python_uploads(signed_repo, monkeypatch):
    paths = {
        ".changes/v0.8.2.md": "## [0.8.2] - 2026-11-16\n",
        "libs/core/pyproject.toml": '[project]\nversion="0.3.10"\n',
        "libs/meta/pyproject.toml": '[project]\nversion="0.4.7"\n',
        "libs/spec/package.json": '{"version":"0.4.1"}',
    }
    for name, content in paths.items():
        path = signed_repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    output = signed_repo / "outputs"
    for name, value in {
        "EVENT_NAME": "workflow_dispatch",
        "INPUT_DRY_RUN": "true",
        "INPUT_VALIDATION_ONLY": "true",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(name, value)
    write_plan(signed_repo)
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["upload_python"] == "false"
    assert values["dry_run"] == "true"
    assert values["wave"] == "0.8.2"
    assert values["core"] == "0.3.10"
    assert values["umbrella"] == "0.4.7"
    assert values["source_sha"] == run(signed_repo, "git", "rev-parse", "HEAD").stdout.strip()


def test_revoked_key_in_main_cannot_authorize_old_tag(signed_repo):
    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    other = signed_repo / "replacement"
    run(signed_repo, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other))
    (signed_repo / ".github/release-signers").write_text(
        "release@example.invalid " + other.with_suffix(".pub").read_text()
    )
    run(signed_repo, "git", "add", ".github/release-signers")
    run(signed_repo, "git", "commit", "-m", "rotate signing trust")
    with pytest.raises(ReleaseGateError):
        verify_tag(signed_repo, "v1.2.3", "main")


def test_validation_only_cannot_silently_become_production():
    with pytest.raises(ReleaseGateError, match="validation_only"):
        release_mode("workflow_dispatch", "false", "true")


def plan_fixture(repo, monkeypatch, wave="1.2.3"):
    paths = {
        f".changes/v{wave}.md": f"## [{wave}] - 2026-11-17\n",
        "libs/core/pyproject.toml": '[project]\nversion="0.3.10"\n',
        "libs/meta/pyproject.toml": '[project]\nversion="0.4.7"\n',
        "libs/spec/package.json": '{"version":"0.4.1"}',
    }
    for name, content in paths.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\nprintf \'[[{"title":"v1.2.3","state":"closed","open_issues":0}]]\\n\'\n'
    )
    gh.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    for name, value in {
        "EVENT_NAME": "release",
        "RELEASE_TAG": "v1.2.3",
        "GITHUB_OUTPUT": str(repo / "outputs"),
        "GITHUB_REPOSITORY": "backblaze-labs/genblaze",
    }.items():
        monkeypatch.setenv(name, value)
    run(repo, "git", "update-ref", "refs/remotes/origin/main", "HEAD")


def test_production_rejects_head_different_from_signed_tag(signed_repo, monkeypatch):
    from release_gate import production

    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    (signed_repo / "later").touch()
    run(signed_repo, "git", "add", "later")
    run(signed_repo, "git", "commit", "-m", "later main commit")
    plan_fixture(signed_repo, monkeypatch)
    with pytest.raises(ReleaseGateError, match="candidate differs"):
        production(signed_repo, "v1.2.3", "backblaze-labs/genblaze")


def test_release_tag_must_match_wave(signed_repo, monkeypatch):
    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    plan_fixture(signed_repo, monkeypatch, wave="2.0.0")
    with pytest.raises(ReleaseGateError, match="differs from the latest Changie wave"):
        write_plan(signed_repo)


def test_complete_production_plan_enables_upload_only_after_trust(signed_repo, monkeypatch):
    run(signed_repo, "git", "tag", "-s", "v1.2.3", "-m", "release")
    plan_fixture(signed_repo, monkeypatch)
    write_plan(signed_repo)
    assert "upload_python=true\n" in (signed_repo / "outputs").read_text()


def test_wrong_wave_milestone_does_not_satisfy_readiness():
    with pytest.raises(ReleaseGateError, match="v1.2.3"):
        check_milestone([{"title": "v9.9.9", "state": "closed", "open_issues": 0}], "1.2.3")


def test_explicit_production_dispatch_mode():
    assert release_mode("workflow_dispatch", "false", "false") == (False, False)


@pytest.mark.parametrize("value", ["", "TRUE", "1", "false; echo nope"])
def test_invalid_validation_only_boolean_fails_closed(value):
    with pytest.raises(ReleaseGateError):
        release_mode("workflow_dispatch", "true", value)
