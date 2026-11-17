"""Tests for .claude/hooks/batch-executor-guard.py.

The hook is the runtime boundary for batch executors, which act on untrusted
issue text. It must: stay silent for every other agent; confine executor Bash
to a kernel sandbox with no network, no credential reads and writes only to
its worktree; and refuse file tools outside the worktree or on guarded paths.
The sandbox tests actually execute the rewritten command, so they assert what
the kernel enforces, not what the command string looks like.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "batch-executor-guard.py"

needs_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sandbox-exec"),
    reason="the executor sandbox uses macOS sandbox-exec",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A main checkout plus a linked worktree, as the workflow runtime creates."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.name", "Test Bot")
    _git(main, "config", "user.email", "test@example.com")
    (main / "README.md").write_text("hi", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init", "--no-gpg-sign")
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "fix/issue-70", str(worktree))
    home = tmp_path / "home"
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("oauth_token: SECRET", encoding="utf-8")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("KEY", encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("CODEXSECRET", encoding="utf-8")
    (home / ".b2_account_info").write_text("B2SECRET", encoding="utf-8")
    return {"main": main, "worktree": worktree, "home": home, "tmp": tmp_path}


def _run_hook(payload: dict | str, home: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)], input=data, capture_output=True, text=True, env=env
    )


def _payload(tool: str, tool_input: dict, cwd: Path, agent_type: str = "batch-executor") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "agent_type": agent_type,
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": str(cwd),
    }


def _decision(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else {}


def _sandboxed(repo: dict, command: str) -> subprocess.CompletedProcess:
    """Rewrite ``command`` through the hook, then actually run the result."""
    out = _decision(
        _run_hook(_payload("Bash", {"command": command}, repo["worktree"]), repo["home"])
    )
    assert out["permissionDecision"] == "allow"
    rewritten = out["updatedInput"]["command"]
    env = dict(os.environ, HOME=str(repo["home"]), GH_TOKEN="ghp_SHOULD_NOT_LEAK")  # noqa: S106
    return subprocess.run(
        ["/bin/bash", "-c", rewritten],
        cwd=repo["worktree"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_type", [None, "", "general-purpose", "genblaze-maintainer"])
def test_other_agents_are_untouched(repo, agent_type):
    payload = _payload("Bash", {"command": "gh release list"}, repo["main"], agent_type or "")
    if agent_type is None:
        payload.pop("agent_type")
    proc = _run_hook(payload)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_malformed_input_fails_closed():
    proc = _run_hook("not json")
    assert proc.returncode == 2


def test_executor_outside_a_linked_worktree_is_denied(repo):
    out = _decision(_run_hook(_payload("Bash", {"command": "ls"}, repo["main"])))
    assert out["permissionDecision"] == "deny"
    assert "worktree" in out["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# file tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".github/workflows/x.yml",
        ".claude/settings.json",
        "CHANGELOG.md",
        "libs/core/pyproject.toml",
    ],
)
def test_edits_to_guarded_paths_are_denied(repo, rel):
    target = repo["worktree"] / rel
    out = _decision(_run_hook(_payload("Write", {"file_path": str(target)}, repo["worktree"])))
    assert out["permissionDecision"] == "deny"


def test_edits_inside_worktree_pass_through(repo):
    target = repo["worktree"] / "libs" / "core" / "a.py"
    assert (
        _decision(_run_hook(_payload("Edit", {"file_path": str(target)}, repo["worktree"]))) == {}
    )


@pytest.mark.parametrize("tool", ["Write", "Edit", "Read"])
def test_file_tools_outside_worktree_are_denied(repo, tool):
    target = repo["home"] / ".config" / "gh" / "hosts.yml"
    out = _decision(_run_hook(_payload(tool, {"file_path": str(target)}, repo["worktree"])))
    assert out["permissionDecision"] == "deny"


def test_symlink_escape_is_denied(repo):
    link = repo["worktree"] / "escape"
    link.symlink_to(repo["home"])
    target = link / ".config" / "gh" / "hosts.yml"
    out = _decision(_run_hook(_payload("Read", {"file_path": str(target)}, repo["worktree"])))
    assert out["permissionDecision"] == "deny"


def test_grep_outside_worktree_is_denied(repo):
    out = _decision(_run_hook(_payload("Grep", {"pattern": "x", "path": "/"}, repo["worktree"])))
    assert out["permissionDecision"] == "deny"


def test_glob_pattern_with_parent_segments_is_denied(repo):
    out = _decision(_run_hook(_payload("Glob", {"pattern": "../main/**/*"}, repo["worktree"])))
    assert out["permissionDecision"] == "deny"


def test_reading_issue_files_is_allowed(repo):
    issue = repo["main"] / ".batch" / "issues" / "70.json"
    assert (
        _decision(_run_hook(_payload("Read", {"file_path": str(issue)}, repo["worktree"]))) == {}
    )


# ---------------------------------------------------------------------------
# Bash sandbox (executed, not just inspected)
# ---------------------------------------------------------------------------


@needs_sandbox
def test_sandbox_allows_normal_work(repo):
    proc = _sandboxed(
        repo,
        "echo 'x = 1' > a.py && git add a.py && git commit -qm 'fix: a' && python3 -c 'print(1)'",
    )
    assert proc.returncode == 0, proc.stderr
    assert "fix: a" in _git(repo["worktree"], "log", "-1", "--format=%s")


@needs_sandbox
def test_sandbox_strips_credentials_from_env(repo):
    proc = _sandboxed(repo, "env")
    assert proc.returncode == 0
    assert "ghp_SHOULD_NOT_LEAK" not in proc.stdout


_WRITE_AND_RUN_NETWORK_SCRIPT = (
    "printf 'import urllib.request\\n"
    + 'urllib.request.urlopen("https://github.com", timeout=3)\\n'
    + "' > t.py && python3 t.py"
)


@needs_sandbox
@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import socket; socket.create_connection((\"140.82.112.3\", 443), timeout=3)'",
        "curl -s -m 3 https://github.com",
        'cat "$HOME/.config/gh/hosts.yml"',
        'cat "$HOME/.ssh/id_ed25519"',
        "echo x > ../outside.txt",
        'echo x > "$HOME/.zshrc"',
        "git -C ../main config user.name pwned",
        'echo x > "$(git rev-parse --git-common-dir)/hooks/pre-commit"',
        "git update-ref refs/remotes/origin/main HEAD",
        'cat "$HOME/.codex/auth.json"',
        'cat "$HOME/.b2_account_info"',
        "git update-ref refs/heads/main HEAD",
        'echo junk > "$(git rev-parse --git-dir)/commondir"',
        "echo 'gitdir: /tmp/x' > .git",
        "/usr/bin/osascript -e 'return 1'",
        'echo /tmp > "$(git rev-parse --git-common-dir)/objects/info/alternates"',
        # A script the agent writes inherits the sandbox when run.
        _WRITE_AND_RUN_NETWORK_SCRIPT,
    ],
)
def test_sandbox_denies_escape(repo, command):
    proc = _sandboxed(repo, command)
    assert proc.returncode != 0, proc.stdout
    assert not (repo["tmp"] / "outside.txt").exists()
    assert "pwned" not in _git(repo["main"], "config", "--get", "user.name")


@needs_sandbox
def test_sandbox_can_commit_on_batch_branch_namespace(repo):
    proc = _sandboxed(
        repo, "git switch -q -c fix/issue-71 && echo y > b.py && git add b.py && git commit -qm b"
    )
    assert proc.returncode == 0, proc.stderr
    assert "b" in _git(repo["main"], "log", "-1", "--format=%s", "fix/issue-71")


@needs_sandbox
def test_main_branch_is_not_moved(repo):
    before = _git(repo["main"], "rev-parse", "main")
    _sandboxed(repo, "git commit -q --allow-empty -m x && git update-ref refs/heads/main HEAD")
    assert _git(repo["main"], "rev-parse", "main") == before


@needs_sandbox
def test_unsetting_env_inside_the_command_does_not_escape(repo):
    proc = _sandboxed(repo, 'unset GH_CONFIG_DIR; cat "$HOME/.config/gh/hosts.yml"')
    assert proc.returncode != 0
    assert "SECRET" not in proc.stdout


@needs_sandbox
def test_git_works_when_the_repo_lives_under_home(tmp_path):
    """Maintainers keep repos under $HOME, which the sandbox otherwise denies."""
    home = tmp_path / "home"
    main = home / "Documents" / "src" / "main"
    main.mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.name", "Test Bot")
    _git(main, "config", "user.email", "test@example.com")
    (main / "README.md").write_text("hi", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init", "--no-gpg-sign")
    (home / "Documents" / "notes.txt").write_text("PRIVATE", encoding="utf-8")
    worktree = main / ".claude" / "worktrees" / "agent-1"
    _git(main, "worktree", "add", "-q", "-b", "fix/issue-70", str(worktree))
    repo = {"main": main, "worktree": worktree, "home": home, "tmp": tmp_path}

    proc = _sandboxed(
        repo, "git status --short && echo y > b.py && git add b.py && git commit -qm b"
    )
    assert proc.returncode == 0, proc.stderr
    assert _sandboxed(repo, "cat ../../../../notes.txt").returncode != 0
