#!/usr/bin/env python3
"""
Git steward for the batch maintainer: safe preflight + conservative cleanup.

Why this exists
---------------
The batch maintainer spawns work across many branches and worktrees. Before it
starts, the local repo must be on a current ``main`` and clean; after it runs,
the leftover branches/worktrees must be tidied. Doing either carelessly risks
destroying uncommitted or unpushed work — exactly what the user forbade. So all
git mutation lives here, in plain Python under ``make test`` (real throwaway
repos, mirroring ``test_prepare_release.py``), never in an LLM agent that could
freelance a ``git branch -D``.

Two hard safety rules, enforced by construction:

* **preflight never touches your checked-out branch.** No switch, no stash, no
  auto-commit. It fetches refs, reports divergence, and fast-forwards ``main``
  *only* when ``main`` is not the checked-out branch and is strictly behind
  (a pure ref update via ``git fetch origin main:main``). Anything else — dirty
  tree, ``main`` checked out and behind, diverged — stops and reports.

* **gc only ever runs ``git branch -d`` (safe), never ``-D`` (force).** A branch
  whose PR merged by *squash* is not fast-forward-merged into local ``main``, so
  ``-d`` refuses; we report it for manual removal rather than force-deleting.
  Dirty worktrees are skipped. Non-matching branches are never touched.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

# Branch namespaces the maintainer owns. gc only ever considers these; anything
# else (your feature branches, main) is out of scope and never touched.
DEFAULT_BRANCH_PATTERNS: tuple[str, ...] = (
    "issue-*",
    "*/issue-*",
    "issues-*",
    "*/issues-*",
    "cluster-*",
    "cluster/*",
)


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", repo, *args], check=check, capture_output=True, text=True)


def _has_ref(repo: str, ref: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def _ahead_behind(repo: str, local: str, remote: str) -> tuple[int, int]:
    out = _git(repo, "rev-list", "--left-right", "--count", f"{local}...{remote}").stdout
    ahead, behind = out.split()
    return int(ahead), int(behind)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@dataclass
class Preflight:
    ok: bool
    current_branch: str
    dirty: bool
    main_action: str  # up-to-date | fast-forwarded | blocked-* | no-remote
    ahead: int = 0
    behind: int = 0
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def preflight(repo: str) -> Preflight:
    """Verify the repo is safe to start a batch on; sync main without risk.

    Returns a report with ``ok`` gating whether the caller may proceed. Never
    switches branches, stashes, or commits.
    """
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = bool(_git(repo, "status", "--porcelain").stdout.strip())
    messages: list[str] = []

    if dirty:
        messages.append(
            "Working tree has uncommitted changes — commit or stash them "
            "yourself before running a batch (I will not touch your work)."
        )

    if (
        not _has_ref(repo, "refs/remotes/origin/main")
        and not _git(repo, "remote", check=False).stdout.strip()
    ):
        return Preflight(
            ok=False,
            current_branch=current,
            dirty=dirty,
            main_action="no-remote",
            messages=messages + ["No 'origin' remote configured."],
        )

    _git(repo, "fetch", "origin", "--quiet", "--prune", check=False)

    if not _has_ref(repo, "refs/remotes/origin/main"):
        return Preflight(
            ok=False,
            current_branch=current,
            dirty=dirty,
            main_action="no-remote",
            messages=messages + ["origin/main not found after fetch."],
        )

    local_main = _has_ref(repo, "refs/heads/main")
    if not local_main:
        # No local main to get stale; worktrees branch off origin/main directly.
        ok = not dirty
        return Preflight(
            ok=ok,
            current_branch=current,
            dirty=dirty,
            main_action="up-to-date",
            messages=messages + ["No local 'main' branch; using origin/main."],
        )

    ahead, behind = _ahead_behind(repo, "refs/heads/main", "refs/remotes/origin/main")

    if ahead and behind:
        return Preflight(
            ok=False,
            current_branch=current,
            dirty=dirty,
            main_action="blocked-diverged",
            ahead=ahead,
            behind=behind,
            messages=messages
            + [
                f"Local main has diverged from origin/main "
                f"(ahead {ahead}, behind {behind}); resolve by hand."
            ],
        )

    if behind and current == "main":
        return Preflight(
            ok=False,
            current_branch=current,
            dirty=dirty,
            main_action="blocked-checked-out",
            ahead=ahead,
            behind=behind,
            messages=messages
            + [
                f"main is checked out and behind by {behind}; I will not "
                f"fast-forward a checked-out branch. Run `git pull --ff-only`."
            ],
        )

    if behind:
        # Safe pure ref update: main is not checked out and strictly behind.
        _git(repo, "fetch", "origin", "main:main")
        return Preflight(
            ok=not dirty,
            current_branch=current,
            dirty=dirty,
            main_action="fast-forwarded",
            ahead=0,
            behind=behind,
            messages=messages + [f"Fast-forwarded local main by {behind} commit(s)."],
        )

    return Preflight(
        ok=not dirty,
        current_branch=current,
        dirty=dirty,
        main_action="up-to-date",
        ahead=ahead,
        behind=0,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# gc (garbage-collect leftover branches / worktrees)
# ---------------------------------------------------------------------------


@dataclass
class GcResult:
    removed_worktrees: list[str] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    retained: list[dict] = field(default_factory=list)  # {name, reason}
    skipped_dirty: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _worktrees(repo: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into dicts."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    trees: list[dict] = []
    current: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                trees.append(current)
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].replace("refs/heads/", "", 1)
        elif line == "detached":
            current["branch"] = None
    if current:
        trees.append(current)
    return trees


def gc(
    repo: str,
    patterns: tuple[str, ...] = DEFAULT_BRANCH_PATTERNS,
    is_merged: Callable[[str], bool] | None = None,
) -> GcResult:
    """Conservatively remove leftover maintainer worktrees and merged branches.

    ``is_merged(branch)`` reports whether the branch's PR is merged on the
    remote — injected so this is testable without ``gh``. Defaults to a
    gh-backed check for real runs.
    """
    merged = is_merged or _pr_merged_via_gh
    result = GcResult()
    _git(repo, "worktree", "prune")  # drop admin entries for vanished dirs — safe

    toplevel = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    handled_branches: set[str] = set()

    for tree in _worktrees(repo):
        branch = tree.get("branch")
        if tree["path"] == toplevel or not branch or not _matches(branch, patterns):
            continue
        handled_branches.add(branch)
        if _git(tree["path"], "status", "--porcelain", check=False).stdout.strip():
            result.skipped_dirty.append(f"{branch} ({tree['path']})")
            continue
        if not merged(branch):
            result.retained.append({"name": branch, "reason": "PR not merged (in flight)"})
            continue
        # Clean + merged: remove the worktree (no --force), then the branch.
        _git(repo, "worktree", "remove", tree["path"])
        result.removed_worktrees.append(tree["path"])
        _delete_branch(repo, branch, result)

    # Branches not attached to a worktree.
    for line in _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    ).stdout.splitlines():
        branch = line.strip()
        if branch in handled_branches or branch == "main" or not _matches(branch, patterns):
            continue
        if not merged(branch):
            result.retained.append({"name": branch, "reason": "PR not merged (in flight)"})
            continue
        _delete_branch(repo, branch, result)

    return result


def _delete_branch(repo: str, branch: str, result: GcResult) -> None:
    """Delete with safe ``-d`` only; report (never force) if git refuses."""
    proc = _git(repo, "branch", "-d", branch, check=False)
    if proc.returncode == 0:
        result.deleted_branches.append(branch)
    else:
        result.retained.append(
            {
                "name": branch,
                "reason": "PR merged but branch not fast-forward-merged locally "
                "(likely squash-merge); remove manually with "
                f"`git branch -D {branch}` once satisfied.",
            }
        )


def _pr_merged_via_gh(branch: str) -> bool:
    """True if any PR with this head branch is merged (real-run default)."""
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "merged",
            "--json",
            "number",
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False  # conservative: unknown => not merged => retain
    try:
        return bool(json.loads(proc.stdout or "[]"))
    except json.JSONDecodeError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch maintainer git steward.")
    parser.add_argument("--repo", default=".", help="Repo path (default: cwd).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="Check tree is clean and sync main safely.")
    sub.add_parser("gc", help="Remove merged leftover branches/worktrees.")
    args = parser.parse_args(argv)

    if args.cmd == "preflight":
        report = preflight(args.repo)
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if report.ok else 1

    result = gc(args.repo)
    json.dump(result.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
