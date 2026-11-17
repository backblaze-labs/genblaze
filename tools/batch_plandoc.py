#!/usr/bin/env python3
"""
Plan-document writer and drift-guarding validator for the batch maintainer.

Why this exists
---------------
The batch maintainer is split into two workflows with a human approval gate
between them: ``batch-plan`` produces a plan document and stops; the human
reads it and approves; ``batch-execute`` then opens PRs. That gate is only
meaningful if execution doesn't silently run against a stale or accidentally
edited plan. Convention ("please don't") catches nothing. This module adds
mechanical drift checks on top of the human gate:

* ``write`` embeds, in a machine-readable block inside the plan doc, the
  ``origin/main`` SHA the plan was computed against and a **content token** —
  ``sha256(base_sha + canonical(clusters))``. The token binds the approval to
  the exact cluster set; edit the clusters by hand and the token no longer
  matches. Note this is an *unkeyed* digest, so it detects accidental edits
  and drift, not a motivated editor who recomputes it — the human approval
  gate, not this token, is the trust boundary.

* ``render`` adds a ``- [ ] approve `<slug>``` checkbox per executable cluster,
  outside the meta block. Nothing is approved by default: the human ticks the
  clusters they want, and only those run.

* ``validate`` returns the dispatch list (approved, executable clusters with no
  existing PR or branch), or refuses to let execution proceed unless ALL hold:
    1. the embedded token recomputes from the doc's own contents (catches
       accidental edits to the plan block),
    2. at least one cluster is ticked, and every tick names an executable
       cluster,
    3. every approved issue is still open (an issue closed or merged since
       planning aborts the run). Issues opened *after* planning are not a
       drift trigger — they simply wait for the next ``batch-plan``.

  ``origin/main`` advancing since planning is a *warning*, not an abort:
  executors branch off live ``origin/main`` and ``publish`` re-checks every
  diff, so an unrelated merge doesn't force a full re-plan.

Git and GitHub live *outside* this module — the calling workflow supplies the
live SHA and open-issue list — so the logic here is pure and unit-testable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Delimits the machine-readable plan block embedded in the markdown doc. The
# validator extracts exactly what is between these markers.
_META_OPEN = "<!-- BATCH-PLAN-META"
_META_CLOSE = "BATCH-PLAN-META -->"
# Shown above the Approve checklist in every plan doc.
_APPROVAL_HELP = (
    "> To approve, tick the clusters you want under **Approve**, then run "
    "`batch-execute` against this doc (a dry run unless you pass "
    '`"dryRun": false`). Only ticked clusters run. Execution aborts if a '
    "ticked issue has closed; clusters that already have a PR or branch "
    "are skipped. Executors never merge, comment or label."
)
# Cluster slugs as batch_cluster.py emits them. Slugs become branch names and
# shell arguments downstream, so anything else is rejected.
_SLUG_RE = re.compile(r"^issues(?:-\d+)+$")
# Characters that could start markup, a code span, a checkbox or a comment.
_INERT = str.maketrans({"<": "‹", ">": "›", "`": "'", "[": "(", "]": ")"})
# A ticked approval line, e.g. "- [x] approve `issues-70-71`".
_APPROVAL_RE = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*approve\s+`([a-z0-9-]+)`\s*$", re.MULTILINE)


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


def _inert(text: object, limit: int = 200) -> str:
    """Render untrusted text as one short line that can't form markdown structure.

    Skip reasons and notes can carry text quoted from GitHub issues. Collapsing
    whitespace (so it can never start a new line such as a checkbox) and
    neutralizing ``< > ` [ ]`` (comments, code spans, checkboxes) keeps attacker
    text from approving a cluster or hiding content in the rendered doc.
    """
    collapsed = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())
    flat = collapsed.translate(_INERT)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _check_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise ValueError(f"malformed cluster slug {_inert(slug, 60)!r}")
    return slug


def render_doc(plan: dict, base_sha: str, date: str) -> str:
    """Render the human-facing markdown plan doc with an embedded meta block."""
    clusters = plan["clusters"]
    for c in clusters:
        _check_slug(c["slug"])
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
    exec_issue_count = sum(len(c["issues"]) for c in exec_clusters)

    lines: list[str] = [
        f"# Batch Maintainer Plan — {date}",
        "",
        f"- **Base commit** (`origin/main`): `{base_sha}`",
        f"- **Approval token**: `{token}`",
        f"- **Executable now**: {len(exec_clusters)} cluster(s) — {exec_issue_count} issue(s)",
        f"- **Deferred to re-plan**: {len(deferred)} cluster(s)",
        "",
        _APPROVAL_HELP,
        "",
        "## Approve",
        "",
    ]
    lines += [f"- [ ] approve `{c['slug']}`" for c in exec_clusters] or ["_Nothing to approve._"]
    lines += [
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
            f"{len(c['touched_files'])} | {_inert(', '.join(c['blocked_by'])) or '—'} |"
        )

    lines += ["", "## Deferred to a later re-plan", ""]
    if deferred:
        lines += ["| Cluster | Issues | Reason | Blocked by |", "| --- | --- | --- | --- |"]
        for c in deferred:
            issues = ", ".join(f"#{n}" for n in c["issues"])
            lines.append(
                f"| `{c['slug']}` | {issues} | {_inert(c['defer_reason'])} | "
                f"{_inert(', '.join(c['blocked_by'])) or '—'} |"
            )
    else:
        lines.append("_None._")

    lines += ["", "## Skipped issues", ""]
    if meta["skipped"]:
        for s in meta["skipped"]:
            lines.append(f"- #{int(s['number'])} — {_inert(s['reason'])}")
    else:
        lines.append("_None._")

    lines += ["", "## Notes", ""]
    if meta["notes"]:
        lines += [f"- {_inert(n, 400)}" for n in meta["notes"]]
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
    """Extract the embedded meta block from a plan doc.

    Raises unless there is exactly one block: a second one could only come from
    an edit or injected text, and choosing between them would let it win.
    """
    pattern = re.escape(_META_OPEN) + r"\s*(.*?)\s*" + re.escape(_META_CLOSE)
    matches = re.findall(pattern, markdown, re.DOTALL)
    if not matches:
        raise ValueError("plan doc has no BATCH-PLAN-META block")
    if len(matches) > 1:
        raise ValueError(
            f"plan doc must have exactly one BATCH-PLAN-META block, found {len(matches)}"
        )
    return json.loads(matches[0])


def parse_approvals(markdown: str) -> set[str]:
    """Return the cluster slugs ticked in the doc's ``## Approve`` section.

    Only that section counts: ticks anywhere else (notes, skip reasons, the meta
    block) are ignored, so text quoted from an issue can't approve anything.
    """
    pattern = re.escape(_META_OPEN) + r".*?" + re.escape(_META_CLOSE)
    lines = re.sub(pattern, "", markdown, flags=re.DOTALL).splitlines()
    try:
        start = lines.index("## Approve") + 1
    except ValueError:
        return set()
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return set(_APPROVAL_RE.findall("\n".join(lines[start:end])))


class ValidationError(Exception):
    """Raised when a plan doc must not be executed."""


@dataclass
class Dispatch:
    dispatch: list[dict]  # {slug, issues, pr_mode} per cluster to run, in plan order
    warnings: list[str] = field(default_factory=list)


def validate(
    meta: dict,
    live_sha: str,
    live_open_issues: list[int],
    approved: set[str],
    in_flight: set[int],
) -> Dispatch:
    """Return the clusters to run, or raise ValidationError if the plan can't run.

    Pure: the caller supplies the live SHA, open issues, ticked slugs and the
    issues that already have a PR or branch.
    """
    clusters = meta.get("clusters", [])

    # 1. Tamper check: the token must recompute from the doc's own contents.
    expected = compute_token(meta.get("base_sha", ""), clusters)
    if meta.get("token") != expected:
        raise ValidationError(
            "approval token does not match plan contents — the doc was "
            "hand-edited after it was generated; re-run batch-plan."
        )

    # 2. Approval: only ticked, executable clusters may run.
    slugs = [c["slug"] for c in clusters]
    if len(slugs) != len(set(slugs)):
        raise ValidationError("plan has duplicate cluster slugs; re-run batch-plan.")
    bad = sorted(s for s in [*slugs, *approved] if not _SLUG_RE.match(s))
    if bad:
        raise ValidationError(f"malformed cluster slug(s) {[_inert(s, 60) for s in bad]}")
    executable = {c["slug"]: c for c in clusters if c.get("executable")}
    if not approved:
        raise ValidationError(
            "No clusters are approved. Tick the box (- [x] approve <slug>) for "
            "each cluster to run, then re-run batch-execute."
        )
    not_runnable = sorted(approved - set(executable))
    if not_runnable:
        raise ValidationError(
            f"approved cluster(s) {', '.join(not_runnable)} are not executable in "
            "this plan; untick them or re-run batch-plan."
        )

    # 3. Issue drift: every approved issue must still be open.
    chosen = [c for c in clusters if c.get("executable") and c["slug"] in approved]
    closed = {n for c in chosen for n in c["issues"]} - set(live_open_issues)
    if closed:
        raise ValidationError(
            f"approved issues no longer open: "
            f"{', '.join(f'#{n}' for n in sorted(closed))}; re-run batch-plan."
        )

    warnings: list[str] = []
    if meta.get("base_sha") != live_sha:
        warnings.append(
            f"origin/main advanced since planning ({str(meta.get('base_sha'))[:12]} -> "
            f"{live_sha[:12]}); executors branch off live main and publish re-checks "
            "each diff, but expect rebases if the new commits touch the same files."
        )

    runnable = []
    for c in chosen:
        busy = sorted(set(c["issues"]) & in_flight)
        if busy:
            warnings.append(
                f"skipping {c['slug']}: {', '.join(f'#{n}' for n in busy)} already "
                "has a PR or branch (re-run after a partial batch)."
            )
            continue
        runnable.append(
            {"slug": c["slug"], "issues": sorted(c["issues"]), "pr_mode": c["pr_mode"]}
        )
    return Dispatch(dispatch=runnable, warnings=warnings)


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


def _load_issue_state(issues_dir: str) -> tuple[list[int], set[int]]:
    """Open issues and in-flight issues from a fresh ``batch_issues.py`` fetch."""
    open_issues: list[int] = []
    in_flight: set[int] = set()
    for path in sorted(Path(issues_dir).glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        open_issues.append(int(row["number"]))
        if row.get("existing_work"):
            in_flight.add(int(row["number"]))
    return open_issues, in_flight


def _cmd_validate(args: argparse.Namespace) -> int:
    with open(args.doc, encoding="utf-8") as fh:
        markdown = fh.read()
    open_issues, in_flight = _load_issue_state(args.issues_dir)
    try:
        result = validate(
            parse_doc(markdown), args.sha, open_issues, parse_approvals(markdown), in_flight
        )
    except ValidationError as exc:
        print(f"PLAN INVALID: {exc}", file=sys.stderr)
        return 1
    json.dump({"dispatch": result.dispatch, "warnings": result.warnings}, sys.stdout, indent=2)
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
    v.add_argument(
        "--issues-dir",
        required=True,
        help="Fresh tools/batch_issues.py output (open issues + existing work).",
    )
    v.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
