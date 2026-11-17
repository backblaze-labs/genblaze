#!/usr/bin/env python3
"""
Fetch open issues for the batch maintainer, as data rather than prompt text.

Issue titles, bodies and comments are written by anyone, so they are an
injection vector. This tool is the only place that reads them from GitHub. It
writes one JSON file per issue for read-only scouts to open, instead of the
workflow pasting issue text into agent prompts, and it records existing work
(linked PRs, matching branches) deterministically so no agent needs ``gh``.

It also refuses to silently truncate: if there are more open issues than
``--max``, it fails rather than planning a partial set.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# GitHub's closing keywords, followed by "#N".
_LINK_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)
# Branch names the batch maintainer creates: fix/issue-N, cluster/issues-N-M-...
_BRANCH_RE = re.compile(r"(?:^|/)(?:fix/)?issues?-((?:\d+-)*\d+)$")

ISSUE_FIELDS = "number,title,body,author,labels,updatedAt,comments"


class FetchError(RuntimeError):
    pass


def _branch_issues(branch: str) -> set[int]:
    match = _BRANCH_RE.search(branch.strip())
    return {int(n) for n in match.group(1).split("-")} if match else set()


def find_existing_work(number: int, prs: list[dict], branches: list[str]) -> list[str]:
    """Describe in-flight work for issue ``number``: linking PRs and its branches."""
    work: list[str] = []
    for pr in prs:
        if number in {int(n) for n in _LINK_RE.findall(pr.get("body") or "")}:
            work.append(f"PR #{pr['number']} links #{number}")
    heads = [pr.get("headRefName") or "" for pr in prs]
    for branch in [*branches, *heads]:
        name = branch.strip()
        entry = f"branch {name}"
        if name and number in _branch_issues(name) and entry not in work:
            work.append(entry)
    return work


def _default_run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def fetch(max_issues: int, run: Callable[[list[str]], str] = _default_run) -> list[dict]:
    """Return every open issue as a normalized row, or raise if there are > max."""
    issues = json.loads(
        run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--json",
                ISSUE_FIELDS,
                "--limit",
                str(max_issues + 1),
            ]
        )
    )
    if len(issues) > max_issues:
        raise FetchError(
            f"There are more than {max_issues} open issues; raise --max or plan a "
            "subset explicitly. Refusing to plan a silently truncated list."
        )
    prs = json.loads(
        run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,body,headRefName",
                "--limit",
                "500",
            ]
        )
    )
    branches = run(["git", "branch", "-a", "--format=%(refname:short)"]).splitlines()

    rows = []
    for issue in sorted(issues, key=lambda i: i["number"]):
        rows.append(
            {
                "number": issue["number"],
                "title": issue.get("title") or "",
                "author": (issue.get("author") or {}).get("login", ""),
                "labels": [label["name"] for label in issue.get("labels") or []],
                "updated_at": issue.get("updatedAt"),
                "body": issue.get("body") or "",
                "comments": [
                    {
                        "author": (c.get("author") or {}).get("login", ""),
                        "body": c.get("body") or "",
                    }
                    for c in issue.get("comments") or []
                ],
                "existing_work": find_existing_work(issue["number"], prs, branches),
            }
        )
    return rows


def write_issue_files(rows: list[dict], out_dir: Path) -> list[dict]:
    """Write ``<out_dir>/<number>.json`` per issue; return the index for the workflow."""
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for row in rows:
        path = out_dir / f"{row['number']}.json"
        path.write_text(json.dumps(row, indent=2), encoding="utf-8")
        index.append(
            {"number": row["number"], "path": str(path), "existing_work": row["existing_work"]}
        )
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch open issues for batch planning.")
    parser.add_argument("--out-dir", required=True, help="Directory for per-issue JSON files.")
    parser.add_argument("--max", type=int, default=200, help="Fail above this many open issues.")
    args = parser.parse_args(argv)
    try:
        index = write_issue_files(fetch(args.max), Path(args.out_dir))
    except FetchError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    json.dump(index, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
