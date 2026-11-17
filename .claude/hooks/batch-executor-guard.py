#!/usr/bin/env python3
"""
PreToolUse guard for the ``batch-executor`` agent.

Batch executors act on issue text written by anyone on the internet, so they
are treated as untrusted code runners. This hook is registered project-wide in
``.claude/settings.json`` but does nothing unless the calling agent is a
``batch-executor``; every other session and agent is untouched.

For an executor it enforces, by tool:

* **Bash**: the command is re-run inside a macOS ``sandbox-exec`` profile with
  an emptied environment, no network, no reads of credential stores, and
  writes only to the agent's worktree, a per-worktree temp dir and the git
  metadata a commit needs. The sandbox is kernel-enforced and inherited by
  every child process, so a script or test the agent writes cannot escape it
  (``unset``-ing variables inside the command changes nothing).
* **Read / Grep / Glob**: only inside the worktree (and the fetched issue
  files under ``.batch/issues``).
* **Edit / Write / MultiEdit / NotebookEdit**: only inside the worktree and
  never on a path in ``tools/batch_policy.py``.
* **Anything else** (Agent, WebFetch, …): denied.

It fails closed: unreadable input exits 2, and an executor that is not running
in a linked worktree, or on a platform without ``sandbox-exec``, is denied.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from batch_policy import is_dependency_file, is_guarded_path  # noqa: E402

AGENT_TYPE = "batch-executor"
# Branch prefixes an executor may create or move (see batch-execute.js branchFor).
BRANCH_NAMESPACES = ("fix", "cluster")

# Ways to act outside the sandbox through other apps or the keychain.
BLOCKED_BINARIES = ("/usr/bin/osascript", "/usr/bin/open", "/usr/bin/security")
BLOCKED_MACH_SERVICES = (
    "com.apple.SecurityServer",
    "com.apple.securityd",
    "com.apple.coreservices.appleevents",
    "com.apple.coreservices.launchservicesd",
    "com.apple.coreservices.quarantine-resolver",
)
FILE_READ_TOOLS = {"Read", "Grep", "Glob"}
FILE_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# No file or network access: how a workflow agent returns its result.
PASS_THROUGH_TOOLS = {"StructuredOutput"}

# Home-relative credential stores the sandbox may not read.
SECRET_HOME_PATHS = (
    ".config/gh",
    ".config/gcloud",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".netrc",
    ".pypirc",
    ".npmrc",
    ".git-credentials",
    ".claude",
    ".anthropic",
    "Library/Keychains",
)

# Variables passed into the sandbox; everything else (tokens, API keys) is dropped.
PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "PYTHONPATH",
)


def _respond(
    decision: str | None = None, reason: str = "", updated_input: dict | None = None
) -> None:
    out: dict = {"hookEventName": "PreToolUse"}
    if decision:
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = reason
    if updated_input is not None:
        out["updatedInput"] = updated_input
    json.dump({"hookSpecificOutput": out}, sys.stdout)
    sys.exit(0)


def _deny(reason: str) -> None:
    _respond("deny", f"batch-executor guard: {reason}")


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", cwd, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve(raw: str, cwd: Path) -> Path:
    path = Path(os.path.expanduser(raw))
    return (path if path.is_absolute() else cwd / path).resolve()


def _sbpl(path: Path) -> str:
    text = str(path)
    if '"' in text or "\\" in text:
        _deny(f"refusing to sandbox an unusual path: {text!r}")
    return f'"{text}"'


def _home_read_allowlist(home: Path, worktree: Path, main_root: Path) -> list[Path]:
    """Directories under ``home`` the sandbox may read; the rest of home is denied.

    Credentials live in too many places to list (~/.codex, ~/.b2_account_info,
    …), so reads of home are denied by default and only the repo, the
    toolchains on PATH and git's own config are allowed back.
    """
    allowed = [worktree, main_root, home / ".gitconfig", home / ".config" / "git"]
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if os.environ.get(key):
            allowed.append(Path(os.environ[key]).resolve())
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        prefix = Path(entry).resolve().parent  # ~/miniconda3/bin -> ~/miniconda3
        if _inside(prefix, home) and prefix != home:
            allowed.append(prefix)
    return [path for path in allowed if _inside(path, home)]


def _profile(worktree: Path, git_dir: Path, common: Path, run_tmp: Path, home: Path) -> str:
    """Build the sandbox-exec profile. Later rules override earlier ones."""
    main_root = common.parent
    writable = [
        f"(subpath {_sbpl(worktree)})",
        f"(subpath {_sbpl(run_tmp)})",
        f"(subpath {_sbpl(git_dir)})",
        f"(subpath {_sbpl(common / 'objects')})",
        # Only the batch branch namespaces: never main or your own branches.
        *(f"(subpath {_sbpl(common / 'refs' / 'heads' / ns)})" for ns in BRANCH_NAMESPACES),
        *(
            f"(subpath {_sbpl(common / 'logs' / 'refs' / 'heads' / ns)})"
            for ns in BRANCH_NAMESPACES
        ),
        '(literal "/dev/null")',
        '(literal "/dev/tty")',
        '(regex #"^/dev/fd/")',
    ]
    # Files that redirect where git looks for config and hooks; rewriting them
    # would make the next unsandboxed git command in this worktree run code.
    git_pointers = " ".join(
        f"(literal {_sbpl(path)})"
        for path in (
            worktree / ".git",
            git_dir / "commondir",
            git_dir / "gitdir",
            git_dir / "config",
            git_dir / "config.worktree",
        )
    )
    home_reads = " ".join(
        f"(subpath {_sbpl(path)})" for path in _home_read_allowlist(home, worktree, main_root)
    )
    # git and Python stat each parent directory while resolving paths; allow
    # those directory entries alone (``literal``), not their contents.
    ancestors = {p for root in (worktree, main_root) for p in root.parents if _inside(p, home)}
    home_reads += " " + " ".join(f"(literal {_sbpl(path)})" for path in sorted(ancestors))
    secrets = " ".join(f"(subpath {_sbpl(home / rel)})" for rel in SECRET_HOME_PATHS)
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-write*)",
            f"(allow file-write* {' '.join(writable)})",
            f"(deny file-write* (subpath {_sbpl(git_dir / 'hooks')}) {git_pointers})",
            # alternates would point git at an object store the executor controls.
            f"(deny file-write* (subpath {_sbpl(common / 'objects' / 'info')}))",
            f"(deny file-read* (subpath {_sbpl(home)}))",
            f"(allow file-read* (literal {_sbpl(home)}) {home_reads})",
            f"(deny file-read* {secrets})",
            f'(deny file-read* (regex #"^{re.escape(str(main_root))}/\\.env"))',
            # No reaching other apps: Apple Events, LaunchServices, keychain.
            "(deny process-exec "
            + " ".join(f'(literal "{path}")' for path in BLOCKED_BINARIES)
            + ")",
            "(deny mach-lookup "
            + " ".join(f'(global-name "{name}")' for name in BLOCKED_MACH_SERVICES)
            + ")",
        ]
    )


def _sandbox_command(command: str, worktree: Path, git_dir: Path, common: Path) -> str:
    sandbox_exec = shutil.which("sandbox-exec") or (
        "/usr/bin/sandbox-exec" if Path("/usr/bin/sandbox-exec").exists() else None
    )
    if sys.platform != "darwin" or not sandbox_exec:
        _deny("no sandbox-exec on this platform; batch executors only run sandboxed on macOS")
    home = Path(os.environ.get("HOME", str(Path.home()))).resolve()
    digest = hashlib.sha256(str(worktree).encode()).hexdigest()[:12]
    run_tmp = Path(tempfile.gettempdir()).resolve() / f"batch-executor-{digest}"
    run_tmp.mkdir(mode=0o700, exist_ok=True)

    # The environment is scrubbed as the first step *inside* the sandbox,
    # keeping only PASSTHROUGH_ENV. That covers variables the Bash tool adds
    # that this hook never sees, and it never sets HOME or git variables, which
    # Claude Code refuses for worktree-isolated agents (HOME is inherited).
    keep = "|".join(PASSTHROUGH_ENV)
    scrub = (
        f'for _v in $(compgen -e); do case "$_v" in {keep}) ;; '
        '*) unset "$_v" 2>/dev/null ;; esac; done; unset _v; '
        f"export TMPDIR={shlex.quote(str(run_tmp))} TERM=dumb; "
    )
    profile = _profile(worktree, git_dir, common, run_tmp, home)
    return (
        f"{shlex.quote(sandbox_exec)} -p {shlex.quote(profile)} "
        f"/bin/bash -c {shlex.quote(scrub + command)}"
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.stderr.write("batch-executor guard: unreadable hook input\n")
        sys.exit(2)

    if payload.get("agent_type") != AGENT_TYPE:
        sys.exit(0)

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()

    try:
        worktree = Path(_git(str(cwd), "rev-parse", "--show-toplevel")).resolve()
        git_dir = Path(_git(str(cwd), "rev-parse", "--absolute-git-dir")).resolve()
        common = Path(
            _git(str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
    except (subprocess.CalledProcessError, OSError):
        _deny("not inside a git repository")
    if git_dir == common:
        _deny("executors must run in an isolated git worktree, not the main checkout")
    issues_dir = (common.parent / ".batch" / "issues").resolve()

    if tool == "Bash":
        command = tool_input.get("command") or ""
        _respond(
            "allow",
            "sandboxed batch-executor command (no network, writes confined to the worktree)",
            {**tool_input, "command": _sandbox_command(command, worktree, git_dir, common)},
        )

    if tool in PASS_THROUGH_TOOLS:
        sys.exit(0)

    if tool in FILE_READ_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("path")
        pattern = tool_input.get("pattern") or ""
        if tool == "Glob" and ".." in Path(pattern).parts:
            _deny(f"Glob pattern escapes the worktree: {pattern}")
        if tool == "Glob" and pattern.startswith(("/", "~")):
            raw = pattern.split("*", 1)[0] or "/"
        target = _resolve(raw, cwd) if raw else cwd
        if not (_inside(target, worktree) or _inside(target, issues_dir)):
            _deny(f"{tool} outside the worktree: {target}")
        sys.exit(0)

    if tool in FILE_WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        target = _resolve(raw, cwd)
        if not _inside(target, worktree):
            _deny(f"{tool} outside the worktree: {target}")
        rel = target.relative_to(worktree).as_posix()
        if is_guarded_path(rel) or is_dependency_file(rel):
            _deny(f"{rel} is release, CI, agent or dependency config; leave it for a human")
        sys.exit(0)

    _deny(f"tool {tool or '<none>'} is not available to batch executors")


if __name__ == "__main__":
    main()
