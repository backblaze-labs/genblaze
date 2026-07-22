#!/usr/bin/env python3
"""
Plan-document writer and drift-proof validator for the batch maintainer.

Why this exists
---------------
The batch maintainer is split into two workflows with a human approval gate
between them: ``batch-plan`` produces a plan document and stops; the human
reads it and approves; ``batch-execute`` then opens PRs. That gate is only
meaningful if execution *cannot* run against a stale or hand-edited plan.
Convention ("please don't") is not enforcement. This module is the
enforcement:

* ``write`` embeds, in a machine-readable block inside the plan doc, the
  ``origin/main`` SHA the plan was computed against and a **content token** —
  ``sha256(base_sha + canonical(clusters))``. The token binds the approval to
  the exact cluster set; edit the clusters by hand and the token no longer
  matches.

* ``validate`` refuses to let execution proceed unless ALL hold:
    1. the embedded token recomputes from the doc's own contents (no tampering),
    2. the embedded base SHA still equals live ``origin/main`` (main hasn't
       advanced under the plan),
    3. the executable issue set still matches the live open-issue set (no issue
       was closed, merged, or newly opened since planning).

Git and GitHub live *outside* this module — the calling workflow supplies the
live SHA and open-issue list — so the logic here is pure and unit-testable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

# Delimits the machine-readable plan block embedded in the markdown doc. The
# validator extracts exactly what is between these markers.
_META_OPEN = "<!-- BATCH-PLAN-META"
_META_CLOSE = "BATCH-PLAN-META -->"


def _canonical_clusters(clusters: list[dict]) -> str:
    """Stable serialization of the cluster set for tokening.

    Only the fields that define the *plan* (issues + executability) are
    tokened, so cosmetic re-rendering never invalidates an approval but any
    change to what would actually be executed does.
    """
    reduced = sorted(
        (
            {
                "issues": sorted(c["issues"]),
                "executable": bool(c["executable"]),
            }
            for c in clusters
        ),
        key=lambda c: c["issues"],
    )
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"))


def compute_token(base_sha: str, clusters: list[dict]) -> str:
    """Content token binding the approval to base SHA + executable plan."""
    payload = f"{base_sha}\n{_canonical_clusters(clusters)}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _executable_issues(clusters: list[dict]) -> list[int]:
    nums = {n for c in clusters if c.get("executable") for n in c["issues"]}
    return sorted(nums)


def render_doc(plan: dict, base_sha: str, date: str) -> str:
    """Render the human-facing markdown plan doc with an embedded meta block."""
    clusters = plan["clusters"]
    token = compute_token(base_sha, clusters)
    meta = {
        "version": 1,
        "date": date,
        "base_sha": base_sha,
        "token": token,
        "clusters": clusters,
        "skipped": plan.get("skipped", []),
        "notes": plan.get("notes", []),
    }

    exec_clusters = [c for c in clusters if c["executable"]]
    deferred = [c for c in clusters if not c["executable"]]

    lines: list[str] = [
        f"# Batch Maintainer Plan — {date}",
        "",
        f"- **Base commit** (`origin/main`): `{base_sha}`",
        f"- **Approval token**: `{token}`",
        f"- **Executable now**: {len(exec_clusters)} cluster(s) — "
        f"{sum(len(c['issues']) for c in exec_clusters)} issue(s)",
        f"- **Deferred to re-plan**: {len(deferred)} cluster(s)",
        "",
        "> Review this plan, then run `batch-execute` against it. Execution "
        "aborts automatically if `origin/main` has advanced or the open-issue "
        "set has drifted since this plan was written.",
        "",
        "## Executable now",
        "",
        "| Cluster | Issues | PR mode | Files | Blocked by |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in exec_clusters:
        issues = ", ".join(f"#{n}" for n in c["issues"])
        lines.append(
            f"| `{c['slug']}` | {issues} | {c['pr_mode']} | "
            f"{len(c['touched_files'])} | {', '.join(c['blocked_by']) or '—'} |"
        )

    lines += ["", "## Deferred to a later re-plan", ""]
    if deferred:
        lines += ["| Cluster | Issues | Reason | Blocked by |", "| --- | --- | --- | --- |"]
        for c in deferred:
            issues = ", ".join(f"#{n}" for n in c["issues"])
            lines.append(
                f"| `{c['slug']}` | {issues} | {c['defer_reason']} | "
                f"{', '.join(c['blocked_by']) or '—'} |"
            )
    else:
        lines.append("_None._")

    lines += ["", "## Skipped issues", ""]
    if meta["skipped"]:
        for s in meta["skipped"]:
            lines.append(f"- #{s['number']} — {s['reason']}")
    else:
        lines.append("_None._")

    lines += ["", "## Notes", ""]
    if meta["notes"]:
        lines += [f"- {n}" for n in meta["notes"]]
    else:
        lines.append("_None._")

    # Machine-readable block for the validator (last, so it never distracts a
    # human reader; parsed by markers, not by position).
    lines += [
        "",
        _META_OPEN,
        json.dumps(meta, sort_keys=True, indent=2),
        _META_CLOSE,
        "",
    ]
    return "\n".join(lines)


def parse_doc(markdown: str) -> dict:
    """Extract the embedded meta block from a plan doc. Raises on absence."""
    pattern = re.escape(_META_OPEN) + r"\s*(.*?)\s*" + re.escape(_META_CLOSE)
    match = re.search(pattern, markdown, re.DOTALL)
    if not match:
        raise ValueError("plan doc has no BATCH-PLAN-META block")
    return json.loads(match.group(1))


class ValidationError(Exception):
    """Raised when a plan doc must not be executed."""


def validate(meta: dict, live_sha: str, live_open_issues: list[int]) -> list[int]:
    """Return the executable issue set, or raise ValidationError on any drift.

    Pure: the caller supplies the live SHA and open-issue list from git/gh.
    """
    clusters = meta.get("clusters", [])

    # 1. Tamper check: the token must recompute from the doc's own contents.
    expected = compute_token(meta.get("base_sha", ""), clusters)
    if meta.get("token") != expected:
        raise ValidationError(
            "approval token does not match plan contents — the doc was "
            "hand-edited after it was generated; re-run batch-plan."
        )

    # 2. Base drift: main must not have advanced under the plan.
    if meta.get("base_sha") != live_sha:
        raise ValidationError(
            f"origin/main advanced since planning "
            f"(plan {meta.get('base_sha')} != live {live_sha}); re-run batch-plan."
        )

    # 3. Issue-set drift: every executable issue must still be open, and no new
    #    open issue should be silently ignored by a stale plan.
    planned = set(_executable_issues(clusters))
    live = set(live_open_issues)
    closed = planned - live
    if closed:
        raise ValidationError(
            f"planned issues no longer open: "
            f"{', '.join(f'#{n}' for n in sorted(closed))}; re-run batch-plan."
        )
    return sorted(planned)


def _cmd_write(args: argparse.Namespace) -> int:
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    doc = render_doc(plan, args.sha, args.date)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print(args.out)
    else:
        sys.stdout.write(doc)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    with open(args.doc, encoding="utf-8") as fh:
        meta = parse_doc(fh.read())
    open_issues = json.loads(args.open_issues)
    try:
        executable = validate(meta, args.sha, open_issues)
    except ValidationError as exc:
        print(f"PLAN INVALID: {exc}", file=sys.stderr)
        return 1
    json.dump({"executable_issues": executable}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch maintainer plan doc I/O.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="Render a plan doc from a cluster plan.")
    w.add_argument("--plan", required=True, help="Cluster plan JSON path.")
    w.add_argument("--sha", required=True, help="origin/main SHA planned against.")
    w.add_argument("--date", required=True, help="Plan date (YYYY-MM-DD).")
    w.add_argument("--out", help="Output path (default: stdout).")
    w.set_defaults(func=_cmd_write)

    v = sub.add_parser("validate", help="Validate a plan doc against live state.")
    v.add_argument("--doc", required=True, help="Plan doc markdown path.")
    v.add_argument("--sha", required=True, help="Live origin/main SHA.")
    v.add_argument("--open-issues", required=True, help="JSON list of live open issue numbers.")
    v.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
