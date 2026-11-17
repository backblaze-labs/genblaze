"""Tests for tools/batch_issues.py: deterministic intake of untrusted issue text.

Issue and comment text is attacker-controlled, so it is fetched by code and
written to files that read-only scouts open, rather than pasted into prompts.
These tests pin the two places where a bug would silently change what gets
planned: truncation of the issue list, and matching of existing work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import pytest  # noqa: E402
from batch_issues import (  # noqa: E402
    FetchError,
    fetch,
    find_existing_work,
    write_issue_files,
)


def _pr(number: int, body: str = "", head: str = "feature/x") -> dict:
    return {"number": number, "body": body, "headRefName": head}


# ---------------------------------------------------------------------------
# existing-work matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["Closes", "closes", "Fixes", "fixed", "Resolves", "resolve"])
def test_linked_pr_is_existing_work(verb):
    work = find_existing_work(70, [_pr(5, f"{verb} #70")], [])
    assert work == ["PR #5 links #70"]


def test_issue_number_match_is_exact():
    prs = [_pr(5, "Closes #700"), _pr(6, "Closes #7")]
    assert find_existing_work(70, prs, ["fix/issue-700", "cluster/issues-170-171"]) == []


def test_branch_names_are_existing_work():
    branches = ["fix/issue-70", "cluster/issues-69-70-71"]
    assert find_existing_work(70, [], branches) == [
        "branch fix/issue-70",
        "branch cluster/issues-69-70-71",
    ]


def test_open_pr_head_branch_counts():
    assert find_existing_work(70, [_pr(9, "", head="fix/issue-70")], []) == ["branch fix/issue-70"]


def test_unrelated_mentions_are_not_existing_work():
    assert find_existing_work(70, [_pr(5, "See #70 for context")], []) == []


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def _fake_gh(issues: list[dict], prs: list[dict] | None = None, branches: str = ""):
    calls: list[list[str]] = []

    def run(cmd: list[str]) -> str:
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            limit = int(cmd[cmd.index("--limit") + 1])
            return json.dumps(issues[:limit])
        if cmd[:3] == ["gh", "pr", "list"]:
            return json.dumps(prs or [])
        if cmd[:2] == ["git", "branch"]:
            return branches
        raise AssertionError(f"unexpected command {cmd}")

    return run, calls


def _issue(n: int, body: str = "", comments: list[dict] | None = None) -> dict:
    return {
        "number": n,
        "title": f"Issue {n}",
        "body": body,
        "author": {"login": "someone"},
        "labels": [{"name": "bug"}],
        "updatedAt": "2026-11-16T00:00:00Z",
        "comments": comments or [],
    }


def test_fetch_returns_every_issue_under_the_cap():
    run, _ = _fake_gh([_issue(n) for n in range(1, 44)])
    rows = fetch(max_issues=200, run=run)
    assert [r["number"] for r in rows] == list(range(1, 44))


def test_fetch_fails_instead_of_truncating():
    run, _ = _fake_gh([_issue(n) for n in range(1, 44)])
    with pytest.raises(FetchError, match="more than 40"):
        fetch(max_issues=40, run=run)


def test_fetch_normalizes_rows_and_marks_existing_work():
    issues = [_issue(70, "body", [{"author": {"login": "x"}, "body": "a comment"}]), _issue(71)]
    run, _ = _fake_gh(issues, prs=[_pr(5, "Fixes #70")], branches="  fix/issue-71\n")
    rows = {r["number"]: r for r in fetch(max_issues=200, run=run)}
    assert rows[70]["labels"] == ["bug"]
    assert rows[70]["author"] == "someone"
    assert rows[70]["comments"] == [{"author": "x", "body": "a comment"}]
    assert rows[70]["existing_work"] == ["PR #5 links #70"]
    assert rows[71]["existing_work"] == ["branch fix/issue-71"]


def test_write_issue_files_one_file_per_issue(tmp_path):
    rows = [{"number": 70, "title": "t", "body": "b", "existing_work": []}]
    index = write_issue_files(rows, tmp_path)
    assert json.loads((tmp_path / "70.json").read_text())["body"] == "b"
    assert index == [{"number": 70, "path": str(tmp_path / "70.json"), "existing_work": []}]
