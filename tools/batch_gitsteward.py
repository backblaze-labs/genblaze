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
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from batch_policy import find_secrets, is_dependency_file, is_guarded_path

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


# Batch executors can write inside their worktrees, so a worktree's git config
# or hooks may be attacker-controlled. Every git call here disables the config
# keys that execute code (fsmonitor, hooks) so the steward never runs it.
_SAFE_GIT = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_SAFE_GIT, "-C", repo, *args], check=check, capture_output=True, text=True
    )


def _has_ref(repo: str, ref: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def _checked_out_elsewhere(repo: str, branch: str) -> bool:
    """True if ``branch`` is checked out in a worktree other than ``repo``'s."""
    here = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    return any(tree.get("branch") == branch and tree["path"] != here for tree in _worktrees(repo))


def _ahead_behind(repo: str, local: str, remote: str) -> tuple[int, int]:
    out = _git(repo, "rev-list", "--left-right", "--count", f"{local}...{remote}").stdout
    ahead, behind = out.split()
    return int(ahead), int(behind)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


# batch-plan's own untracked output; it must not block the next run.
_BATCH_PLAN_DOC = re.compile(r"^\?\? docs/exec-plans/active/batch-plan-[\w.-]+\.md$")


def _is_batch_plan_output(status_line: str) -> bool:
    return bool(_BATCH_PLAN_DOC.match(status_line))


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
    dirty = any(
        not _is_batch_plan_output(line)
        # -uall lists untracked files individually; the default collapses a new
        # directory to one "?? docs/" line that would hide what is in it.
        for line in _git(repo, "status", "--porcelain", "-uall").stdout.splitlines()
    )
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

    if behind and _checked_out_elsewhere(repo, "main"):
        # git refuses to move a branch checked out in another worktree, and
        # nothing in a batch reads local main (executors branch from
        # origin/main; publish re-fetches it), so report and carry on.
        return Preflight(
            ok=not dirty,
            current_branch=current,
            dirty=dirty,
            main_action="skipped-checked-out-elsewhere",
            ahead=ahead,
            behind=behind,
            messages=messages
            + [
                f"Local main is behind by {behind} but checked out in another "
                "worktree; leaving it alone and using origin/main."
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


# ---------------------------------------------------------------------------
# publish (the only path from an executor's local branch to GitHub)
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    ok: bool
    branch: str
    violations: list[str] = field(default_factory=list)
    pr_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _default_gh(cmd: list[str], stdin: str | None = None) -> str:
    return subprocess.run(cmd, input=stdin, check=True, capture_output=True, text=True).stdout


_COAUTHOR_RE = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE | re.MULTILINE)


def _changed_entries(repo: str, revspec: str) -> list[tuple[str, list[str]]]:
    """Return ``(new_mode, paths)`` per changed entry in ``revspec``.

    Uses ``--raw -z`` so paths come back verbatim: the default output quotes
    names with non-ASCII bytes or ``"``, which would slip past pattern matching.
    ``-M -C`` report renames and copies with both paths, so moving a file out of
    a guarded directory is caught on its old path.
    """
    fields = _git(repo, "diff", "--raw", "-z", "-M", "-C", revspec).stdout.split("\0")
    entries: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(fields) and fields[i].startswith(":"):
        meta = fields[i][1:].split()
        status = meta[4]
        count = 2 if status[0] in "RC" else 1
        entries.append((meta[1], fields[i + 1 : i + 1 + count]))
        i += 1 + count
    return entries


def _show(path: str) -> str:
    """A path as one escaped, bounded line (paths are attacker-chosen)."""
    text = json.dumps(path)[1:-1]
    return text if len(text) <= 120 else text[:119] + "…"


def verify_objects(repo: str) -> list[str]:
    """Return problems if any stored object's content doesn't match its hash.

    Batch executors can write to the shared object store (commits need it), so
    one could overwrite an existing blob in place; the next local checkout or
    merge would then write the attacker's content. ``fsck --full`` re-hashes
    every object, catching a swapped, pre-planted or forged one.
    """
    proc = _git(repo, "fsck", "--full", "--no-dangling", "--no-progress", check=False)
    if proc.returncode == 0:
        return []
    detail = " ".join((proc.stderr or proc.stdout).split())[:300]
    return [f"object store integrity check failed (git fsck): {detail}"]


def branch_violations(repo: str, branch: str, base: str = "origin/main") -> list[str]:
    """Return every reason ``branch`` must not be pushed (empty list = safe).

    Executors work from untrusted issue text and may have changed files through
    Bash, so this re-derives the facts from git instead of trusting a report.
    """
    if not _has_ref(repo, f"refs/heads/{branch}"):
        return [f"branch {branch} does not exist"]
    if _git(repo, "merge-base", "--is-ancestor", base, branch, check=False).returncode != 0:
        return [f"{branch} is not based on {base} (stacked or unrelated history)"]

    commits = _git(repo, "rev-list", f"{base}..{branch}").stdout.split()
    if not commits:
        return [f"{branch} has no commits beyond {base}"]

    violations: list[str] = []
    for new_mode, paths in _changed_entries(repo, f"{base}...{branch}"):
        if new_mode == "120000":
            violations.append(f"adds a symlink {_show(paths[-1])}")
        elif new_mode == "160000":
            violations.append(f"adds a submodule {_show(paths[-1])}")
        for path in paths:
            if is_guarded_path(path):
                violations.append(f"touches guarded path {_show(path)}")
            elif is_dependency_file(path):
                violations.append(f"changes dependency file {_show(path)}")

    messages = _git(repo, "log", "--format=%B", f"{base}..{branch}").stdout
    if _COAUTHOR_RE.search(messages):
        violations.append("a commit carries a Co-authored-by trailer")

    added = [
        line[1:]
        for line in _git(repo, "diff", "--unified=0", f"{base}...{branch}").stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    for secret in find_secrets("\n".join(added)):
        violations.append(f"diff adds a credential-shaped string ({secret})")
    return violations


def _git_with_index(
    repo: str, index: str, *args: str, stdin: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run git against a throwaway index so a worktree's own index is untouched."""
    return subprocess.run(
        ["git", *_SAFE_GIT, "-C", repo, *args],
        input=stdin,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_INDEX_FILE": index},
    )


def _read_summary(worktree: Path) -> str:
    """The executor's one-line summary from ``.batch/summary.txt``, or "".

    Only a regular file is read (never a symlink, which could point at a secret
    and carry it into the commit message), and the text is flattened to one
    bounded printable line.
    """
    path = worktree / ".batch" / "summary.txt"
    if path.is_symlink() or not path.is_file():
        return ""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
        text = fh.read(4096)
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return flat[:299] + "…" if len(flat) > 300 else flat


def commit_worktree_changes(
    repo: str, worktree: str, branch: str, issues: list[int], base: str
) -> list[str]:
    """Commit an executor worktree's changes as ``branch`` on top of ``base``.

    Batch executors can't run git (Claude Code refuses git inside the sandbox
    wrapper), so this trusted step does it. It checks the path is a registered
    batch worktree, snapshots the worktree's files (``.batch/`` always left out),
    and re-applies that change onto the freshly fetched
    base: the runtime may have created the worktree from an older commit. The
    worktree's own index, HEAD and files are never modified. Returns the
    reasons it refused (empty list = ``refs/heads/<branch>`` created).
    """
    common = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).resolve()
    main_root = common.parent
    allowed = main_root / ".claude" / "worktrees"
    wt = Path(worktree).resolve()
    registered = {Path(t["path"]).resolve() for t in _worktrees(repo)}
    if wt == main_root or allowed not in wt.parents or wt not in registered:
        return [f"refusing worktree {_show(str(worktree))}: not a registered batch worktree"]

    with tempfile.TemporaryDirectory() as tmp:
        snapshot, target = str(Path(tmp) / "snapshot"), str(Path(tmp) / "target")
        _git_with_index(str(wt), snapshot, "read-tree", "HEAD")
        # .batch/ holds the executor's summary and fetched issue text: never
        # commit it, whatever the worktree's .gitignore says. It's dropped from
        # the throwaway index after `add` rather than excluded by pathspec,
        # because naming an ignored path makes `git add` exit 1.
        _git_with_index(str(wt), snapshot, "add", "-A", "--", ".")
        _git_with_index(
            str(wt), snapshot, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".batch"
        )
        tree = _git_with_index(str(wt), snapshot, "write-tree").stdout.strip()
        if tree == _git(str(wt), "rev-parse", "HEAD^{tree}").stdout.strip():
            return ["the executor's worktree has no changes"]
        patch = _git(str(wt), "diff", "--binary", "--full-index", "HEAD", tree).stdout

        _git_with_index(repo, target, "read-tree", base)
        applied = _git_with_index(
            repo, target, "apply", "--cached", "--binary", stdin=patch, check=False
        )
        if applied.returncode != 0:
            return [f"the executor's changes don't apply cleanly to {base}"]
        new_tree = _git_with_index(repo, target, "write-tree").stdout.strip()

    refs = ", ".join(f"#{n}" for n in issues)
    message = f"fix: resolve {refs}\n"
    summary = _read_summary(wt)
    if summary:
        message += f"\n{summary}\n"
    commit = subprocess.run(
        ["git", *_SAFE_GIT, "-C", repo, "commit-tree", new_tree, "-p", base, "-F", "-"],
        input=message,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "update-ref", f"refs/heads/{branch}", commit)
    return []


def publish(
    repo: str,
    branch: str,
    issues: list[int],
    base: str = "origin/main",
    gh: Callable[[list[str], str | None], str] = _default_gh,
    worktree: str | None = None,
) -> PublishResult:
    """Verify ``branch``, then push it and open a PR that closes ``issues``.

    Nothing leaves the machine unless every check passes. The PR title and body
    are built from the branch's own commits and the plan's issue numbers, never
    from an agent's free-text report, so no untrusted prose reaches a step that
    holds credentials.
    """
    # Refresh the base from the remote first: a local ``origin/main`` ref is
    # writable by anything that ran in this repo, so it is not trusted as-is.
    remote, _, base_branch = base.partition("/")
    _git(repo, "fetch", "--quiet", remote, f"+refs/heads/{base_branch}:refs/remotes/{base}")
    violations = verify_objects(repo)
    if not violations and worktree is not None:
        violations = commit_worktree_changes(repo, worktree, branch, issues, base)
    violations = violations or branch_violations(repo, branch, base)
    if violations:
        return PublishResult(ok=False, branch=branch, violations=violations)

    subjects = _git(
        repo, "log", "--reverse", "--format=%s", f"{base}..{branch}"
    ).stdout.splitlines()
    bodies = _git(repo, "log", "--reverse", "--format=%B%n---", f"{base}..{branch}").stdout.strip()
    closes = "\n".join(f"Closes #{n}" for n in issues)
    footer = "Opened by `batch-execute`; review before merging."
    body = f"{bodies}\n\n## Related\n\n{closes}\n\n{footer}\n"

    _git(repo, "push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
    out = gh(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base.split("/", 1)[-1],
            "--head",
            branch,
            "--title",
            subjects[0],
            "--body-file",
            "-",
        ],
        body,
    )
    return PublishResult(ok=True, branch=branch, pr_url=out.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch maintainer git steward.")
    parser.add_argument("--repo", default=".", help="Repo path (default: cwd).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="Check tree is clean and sync main safely.")
    sub.add_parser("gc", help="Remove merged leftover branches/worktrees.")
    pub = sub.add_parser("publish", help="Verify a local branch, then push it and open a PR.")
    pub.add_argument("--branch", required=True)
    pub.add_argument("--issues", required=True, help="Comma-separated issue numbers to close.")
    pub.add_argument(
        "--worktree",
        help="An executor's batch worktree: commit its changes as --branch before checking.",
    )
    args = parser.parse_args(argv)

    if args.cmd == "preflight":
        report = preflight(args.repo)
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if report.ok else 1

    if args.cmd == "publish":
        issues = [int(n) for n in args.issues.split(",") if n.strip()]
        published = publish(args.repo, args.branch, issues, worktree=args.worktree)
        json.dump(published.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if published.ok else 1

    result = gc(args.repo)
    json.dump(result.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
