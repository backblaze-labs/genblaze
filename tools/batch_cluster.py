#!/usr/bin/env python3
"""
Deterministic clustering core for the batch maintainer.

Why this exists
---------------
The batch maintainer reviews every open GitHub issue and decides which ones
can be worked together, in what order, and which must wait. That decision
MUST be deterministic and testable — if it lived in an LLM prompt it would
differ every run and there would be no way to unit-test "did we group these
correctly?". So the judgment call ("which files does issue #70 touch?") stays
with the scout agent, but the grouping algorithm that turns those estimates
into an execution plan lives here, in plain Python, under ``make test``.

The safety model (see ``.claude/agents/genblaze-maintainer.md``)
----------------------------------------------------------------
Clustering is *advisory*, not a guarantee of merge-safety — its input is an
LLM's file estimate, which can be wrong. Two independent guards backstop it:

1. **Hot-file exclusion.** Files that almost every change touches (CHANGELOG,
   entry-points, the JSON spec, the AGENTS doc map) are NOT used as overlap
   signal — otherwise every issue would collapse into one giant cluster.
   They are recorded as a serialization *note* for the human instead.
2. **The executor's rebase check.** The genblaze-maintainer opens a draft PR
   if a cluster hits a real conflict at rebase time. Clustering only has to
   be good enough to keep parallel work mostly disjoint; it does not have to
   be perfect.

What this module produces
-------------------------
A plan of *clusters*. Every cluster carries whether it is executable **now**
(dependency layer 0 and within the size cap) or deferred to a re-plan after
its prerequisites merge. v1 deliberately does NOT stack dependent branches on
each other's git base — deeper dependency layers are deferred, not stacked,
because a human squash-merge of a prerequisite would leave a dependent branch
with a stale base (data-loss / phantom-diff trap).

Input (scout JSON, a list of):
    {"number": 70, "type": "bug", "touched_files": ["libs/core/x.py"],
     "deps": [66], "skip_reason": null}

Output (plan JSON): see ``build_plan`` / ``Plan``.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

# Files touched by so many unrelated changes that treating them as "shared
# work" would wrongly merge every issue into one cluster. They are handled as
# a merge-time serialization note, not as a clustering key. fnmatch globs over
# repo-relative POSIX paths.
HOT_FILE_PATTERNS: tuple[str, ...] = (
    "CHANGELOG.md",
    "README.md",
    "AGENTS.md",
    "**/pyproject.toml",
    "pyproject.toml",
    "libs/spec/**",
    "docs/exec-plans/**",
)

# A cluster combined into a single PR must stay reviewable. Above either ceiling
# the overlapping component is treated as inherently serial: only its first
# issue runs now, the rest defer to a re-plan after that PR merges.
DEFAULT_MAX_ISSUES = 3
DEFAULT_MAX_FILES = 12

# defer_reason values (empty string => executable now)
DEFER_AWAITING_PREREQ = "awaiting-prerequisite"
DEFER_OVERSIZED = "oversized-serialize"
DEFER_CYCLE = "dependency-cycle"


@dataclass(frozen=True)
class Config:
    max_issues: int = DEFAULT_MAX_ISSUES
    max_files: int = DEFAULT_MAX_FILES
    hot_patterns: tuple[str, ...] = HOT_FILE_PATTERNS


@dataclass
class Cluster:
    id: str
    slug: str
    issues: list[int]
    touched_files: list[str]  # non-hot files, sorted, deduped
    hot_files: list[str]  # hot files any member touches (serialization note)
    pr_mode: str  # "combined" (>1 issue, one PR) | "single"
    layer: int  # dependency depth; 0 == no prerequisite clusters
    executable: bool  # runnable this batch (layer 0 and within cap)
    blocked_by: list[str] = field(default_factory=list)  # cluster ids
    defer_reason: str = ""


@dataclass
class Plan:
    clusters: list[Cluster]
    skipped: list[dict]  # issues the scout excluded, with reasons
    notes: list[str]  # human-facing warnings (hot-file collisions, cycles)

    def to_dict(self) -> dict:
        return {
            "clusters": [asdict(c) for c in self.clusters],
            "skipped": self.skipped,
            "notes": self.notes,
        }


def _is_hot(path: str, patterns: tuple[str, ...]) -> bool:
    """True if ``path`` matches any hot-file glob (repo-relative POSIX)."""
    p = PurePosixPath(path.strip()).as_posix()
    return any(fnmatch.fnmatch(p, pat) for pat in patterns)


def _normalize(paths: list[str]) -> list[str]:
    """Repo-relative POSIX, whitespace-trimmed, deduped, sorted.

    Normalizing here is what makes clustering deterministic despite the scout's
    free-form file estimates — same set of files always yields the same key.
    """
    seen = {PurePosixPath(p.strip()).as_posix() for p in paths if p.strip()}
    return sorted(seen)


class _UnionFind:
    """Minimal union-find keyed by issue number."""

    def __init__(self, items: list[int]) -> None:
        self._parent = {i: i for i in items}

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Keep the smaller root for deterministic component labels.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo

    def components(self) -> dict[int, list[int]]:
        groups: dict[int, list[int]] = {}
        for item in self._parent:
            groups.setdefault(self.find(item), []).append(item)
        return {root: sorted(members) for root, members in groups.items()}


def _slug(issues: list[int]) -> str:
    return "issues-" + "-".join(str(n) for n in sorted(issues))


def build_plan(scout: list[dict], config: Config | None = None) -> Plan:
    """Turn scout results into a deterministic, dependency-ordered cluster plan.

    Pure function — no I/O, no clock, no git — so it is fully unit-testable.
    """
    cfg = config or Config()
    notes: list[str] = []

    # 1. Partition scouted issues into active vs skipped.
    active: dict[int, dict] = {}
    skipped: list[dict] = []
    for row in scout:
        num = int(row["number"])
        if row.get("skip_reason"):
            skipped.append({"number": num, "reason": row["skip_reason"]})
            continue
        active[num] = row

    # 2. Split each issue's files into overlap-eligible vs hot.
    files: dict[int, list[str]] = {}
    hot: dict[int, list[str]] = {}
    for num, row in active.items():
        norm = _normalize(row.get("touched_files", []))
        files[num] = [p for p in norm if not _is_hot(p, cfg.hot_patterns)]
        hot[num] = [p for p in norm if _is_hot(p, cfg.hot_patterns)]

    # 3. Union issues that share at least one non-hot file.
    uf = _UnionFind(sorted(active))
    owners: dict[str, list[int]] = {}  # file -> issues touching it
    for num, paths in files.items():
        for path in paths:
            owners.setdefault(path, []).append(num)
    for holders in owners.values():
        for other in holders[1:]:
            uf.union(holders[0], other)

    # 3b. Warn about hot files touched by 2+ issues — they merge cleanly far
    # less often than code, so the human should expect a rebase there.
    hot_owners: dict[str, list[int]] = {}
    for num, paths in hot.items():
        for path in paths:
            hot_owners.setdefault(path, []).append(num)
    for path, holders in sorted(hot_owners.items()):
        if len(holders) > 1:
            issues_str = ", ".join(f"#{n}" for n in sorted(holders))
            notes.append(
                f"Hot file `{path}` is touched by {issues_str}; expect a "
                f"manual merge/rebase there even though they are not clustered."
            )

    components = uf.components()  # root -> sorted issue list

    # 4. Map each issue to its component root, then build inter-component
    #    dependency edges from explicit "depends on #x" links.
    root_of = {num: root for root, members in components.items() for num in members}
    dep_edges: dict[int, set[int]] = {
        root: set() for root in components
    }  # child_root <- {prereq_root}
    for num, row in active.items():
        for dep in row.get("deps", []):
            dep = int(dep)
            if dep not in root_of:
                continue  # prereq already merged/closed or out of scope
            child, prereq = root_of[num], root_of[dep]
            if child != prereq:
                dep_edges[child].add(prereq)

    # 5. Assign dependency layers (longest path from a root component). Detect
    #    cycles so we never emit an unschedulable plan.
    layer: dict[int, int] = {}
    cyclic: set[int] = set()

    def _resolve(root: int, stack: tuple[int, ...]) -> int:
        if root in layer:
            return layer[root]
        if root in stack:
            cyclic.update(stack[stack.index(root) :])
            return 0
        prereqs = dep_edges[root]
        depth = 0 if not prereqs else 1 + max(_resolve(p, stack + (root,)) for p in prereqs)
        layer[root] = depth
        return depth

    for root in components:
        _resolve(root, ())

    if cyclic:
        roots = ", ".join(_slug(components[r]) for r in sorted(cyclic))
        notes.append(
            f"Dependency cycle among clusters [{roots}] — marked non-executable; "
            f"resolve the circular 'depends on' links by hand."
        )

    # 6. Emit clusters. A component runs now only if it is dependency layer 0,
    #    not in a cycle, and within the size cap. Oversized overlapping
    #    components are inherently serial: only the first issue runs now.
    clusters: list[Cluster] = []
    for root in sorted(components, key=lambda r: (layer.get(r, 0), r)):
        members = components[root]
        member_files = _normalize([f for n in members for f in files[n]])
        member_hot = _normalize([f for n in members for f in hot[n]])
        blocked_by = sorted(_slug(components[p]) for p in dep_edges[root])
        oversized = len(members) > cfg.max_issues or len(member_files) > cfg.max_files

        if root in cyclic:
            clusters.append(
                _make_cluster(
                    members,
                    member_files,
                    member_hot,
                    layer.get(root, 0),
                    False,
                    blocked_by,
                    DEFER_CYCLE,
                )
            )
        elif oversized:
            # Serialize: run the lowest-numbered issue alone now, defer the rest.
            head, *tail = members
            clusters.append(
                _make_cluster(
                    [head],
                    _normalize(files[head]),
                    _normalize(hot[head]),
                    layer[root],
                    layer[root] == 0 and not blocked_by,
                    blocked_by,
                    "" if layer[root] == 0 and not blocked_by else DEFER_AWAITING_PREREQ,
                )
            )
            clusters.append(
                _make_cluster(
                    tail,
                    _normalize([f for n in tail for f in files[n]]),
                    _normalize([f for n in tail for f in hot[n]]),
                    layer[root],
                    False,
                    blocked_by,
                    DEFER_OVERSIZED,
                )
            )
            notes.append(
                f"Component {_slug(members)} exceeds the size cap "
                f"({len(members)} issues / {len(member_files)} files); running "
                f"#{head} now and deferring the rest to a re-plan after it merges."
            )
        else:
            executable = layer[root] == 0 and not blocked_by
            clusters.append(
                _make_cluster(
                    members,
                    member_files,
                    member_hot,
                    layer[root],
                    executable,
                    blocked_by,
                    "" if executable else DEFER_AWAITING_PREREQ,
                )
            )

    return Plan(clusters=clusters, skipped=skipped, notes=notes)


def _make_cluster(
    issues: list[int],
    files_: list[str],
    hot_: list[str],
    layer_: int,
    executable: bool,
    blocked_by: list[str],
    defer_reason: str,
) -> Cluster:
    issues = sorted(issues)
    return Cluster(
        id=_slug(issues),
        slug=_slug(issues),
        issues=issues,
        touched_files=files_,
        hot_files=hot_,
        pr_mode="combined" if len(issues) > 1 else "single",
        layer=layer_,
        executable=executable,
        blocked_by=blocked_by,
        defer_reason=defer_reason,
    )


def _load_scout(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise SystemExit("scout input must be a JSON list of issue objects")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cluster open issues into a deterministic execution plan."
    )
    parser.add_argument(
        "--scout", required=True, help="Path to scout JSON (list of issue objects)."
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_MAX_ISSUES,
        help="Max issues combined into one PR (default: 3).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="Max distinct files in one combined PR (default: 12).",
    )
    args = parser.parse_args(argv)

    plan = build_plan(
        _load_scout(args.scout),
        Config(max_issues=args.max_issues, max_files=args.max_files),
    )
    json.dump(plan.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
