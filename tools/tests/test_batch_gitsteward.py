"""Tests for tools/batch_gitsteward.py.

Like test_prepare_release.py, these use real throwaway git repositories rather
than mocked git output — the entire value of this module is correct git
plumbing and, above all, NOT destroying work. The tests assert both the happy
paths (sync, tidy) and every refuse-to-act guard (dirty tree, checked-out main,
divergence, squash-merge, dirty worktree, non-matching branch).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from batch_gitsteward import gc, preflight  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, name: str, content: str = "x") -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name}", "--no-gpg-sign")


def _clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True)
    _git(dest, "config", "user.name", "Test Bot")
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "commit.gpgsign", "false")
    return dest


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    _init(origin)
    _commit(origin, "README.md", "hello")
    local = _clone(origin, tmp_path / "local")
    return origin, local


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_clean_and_current_is_ok(tmp_path):
    _, local = _make_origin_and_clone(tmp_path)
    report = preflight(str(local))
    assert report.ok
    assert report.main_action == "up-to-date"
    assert not report.dirty


def test_preflight_dirty_tree_blocks(tmp_path):
    _, local = _make_origin_and_clone(tmp_path)
    (local / "scratch.txt").write_text("wip", encoding="utf-8")
    report = preflight(str(local))
    assert not report.ok
    assert report.dirty
    assert any("uncommitted" in m for m in report.messages)


def test_preflight_fast_forwards_main_when_not_checked_out(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _git(local, "switch", "-q", "-c", "work")  # move off main
    _commit(origin, "feature.py", "new")  # origin/main advances
    report = preflight(str(local))
    assert report.ok
    assert report.main_action == "fast-forwarded"
    # Local main ref now matches origin/main.
    assert (
        _git(local, "rev-parse", "main").strip()
        == _git(local, "rev-parse", "refs/remotes/origin/main").strip()
    )


def test_preflight_refuses_to_ff_checked_out_main(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _commit(origin, "feature.py", "new")  # origin ahead; local still on main
    report = preflight(str(local))
    assert not report.ok
    assert report.main_action == "blocked-checked-out"
    assert report.behind == 1


def test_preflight_blocks_on_divergence(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _commit(local, "local_only.py", "mine")  # local main ahead
    _git(local, "switch", "-q", "-c", "work")  # move off main so it's not "checked out"
    _commit(origin, "theirs.py", "theirs")  # origin ahead too -> diverged
    report = preflight(str(local))
    assert not report.ok
    assert report.main_action == "blocked-diverged"
    assert report.ahead == 1 and report.behind == 1


def test_preflight_no_local_main_uses_origin(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _git(local, "switch", "-q", "-c", "work")
    _git(local, "branch", "-D", "main")
    report = preflight(str(local))
    assert report.ok
    assert "origin/main" in " ".join(report.messages)


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------


def test_gc_deletes_merged_branch(tmp_path):
    _init(tmp_path / "r")
    repo = tmp_path / "r"
    _commit(repo, "README.md")
    # issue-5 fast-forward-merged into main -> git branch -d succeeds.
    _git(repo, "switch", "-q", "-c", "issue-5")
    _commit(repo, "fix.py")
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "issue-5")
    result = gc(str(repo), is_merged=lambda b: b == "issue-5")
    assert "issue-5" in result.deleted_branches


def test_gc_retains_squash_merged_branch_without_force(tmp_path):
    repo = tmp_path / "r"
    _init(repo)
    _commit(repo, "README.md")
    # issue-6 has a commit NOT merged into main; PR "merged" (squash).
    _git(repo, "switch", "-q", "-c", "issue-6")
    _commit(repo, "squashed.py")
    _git(repo, "switch", "-q", "main")
    result = gc(str(repo), is_merged=lambda b: True)
    assert "issue-6" not in result.deleted_branches
    retained = {r["name"] for r in result.retained}
    assert "issue-6" in retained
    # The branch must still exist — never force-deleted.
    assert "issue-6" in _git(repo, "branch", "--format=%(refname:short)")


def test_gc_retains_unmerged_in_flight_branch(tmp_path):
    repo = tmp_path / "r"
    _init(repo)
    _commit(repo, "README.md")
    _git(repo, "branch", "issue-7")
    result = gc(str(repo), is_merged=lambda b: False)
    assert {r["name"] for r in result.retained} == {"issue-7"}
    assert not result.deleted_branches


def test_gc_ignores_non_matching_branches(tmp_path):
    repo = tmp_path / "r"
    _init(repo)
    _commit(repo, "README.md")
    _git(repo, "branch", "my-feature")
    result = gc(str(repo), is_merged=lambda b: True)
    assert not result.deleted_branches
    assert not result.retained
    assert "my-feature" in _git(repo, "branch", "--format=%(refname:short)")


def test_gc_removes_clean_merged_worktree(tmp_path):
    repo = tmp_path / "r"
    _init(repo)
    _commit(repo, "README.md")
    wt = tmp_path / "wt-issue-8"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "issue-8")  # at main HEAD
    result = gc(str(repo), is_merged=lambda b: True)
    assert str(wt) in " ".join(result.removed_worktrees) or any(
        str(wt) in p for p in result.removed_worktrees
    )
    assert "issue-8" in result.deleted_branches
    assert not wt.exists()


def test_gc_skips_dirty_worktree(tmp_path):
    repo = tmp_path / "r"
    _init(repo)
    _commit(repo, "README.md")
    wt = tmp_path / "wt-cluster"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "cluster-foo")
    (wt / "uncommitted.py").write_text("wip", encoding="utf-8")  # dirty
    result = gc(str(repo), is_merged=lambda b: True)
    assert any("cluster-foo" in s for s in result.skipped_dirty)
    assert wt.exists()  # never removed while dirty
