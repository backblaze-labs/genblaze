"""Tests for tools/batch_cluster.py.

The clustering algorithm is the one genuinely deterministic, testable core of
the batch maintainer (the scout's file estimates feeding it are not), so it
carries the load-bearing coverage: disjoint / overlapping / hot-file-only /
dependency-chain / cycle / oversized components, plus input normalization.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from batch_cluster import (  # noqa: E402
    DEFER_AWAITING_PREREQ,
    DEFER_CYCLE,
    DEFER_OVERSIZED,
    Config,
    build_plan,
)


def _issue(
    number: int, files: list[str], deps: list[int] | None = None, skip: str | None = None
) -> dict:
    return {
        "number": number,
        "type": "bug",
        "touched_files": files,
        "deps": deps or [],
        "skip_reason": skip,
    }


def _by_id(plan) -> dict:
    return {c.id: c for c in plan.clusters}


def test_disjoint_issues_are_independent_executable_clusters():
    plan = build_plan(
        [
            _issue(1, ["libs/core/a.py"]),
            _issue(2, ["libs/core/b.py"]),
        ]
    )
    assert len(plan.clusters) == 2
    assert all(c.executable and c.layer == 0 for c in plan.clusters)
    assert all(c.pr_mode == "single" and not c.blocked_by for c in plan.clusters)


def test_overlapping_issues_merge_into_one_combined_cluster():
    plan = build_plan(
        [
            _issue(1, ["libs/core/shared.py", "libs/core/a.py"]),
            _issue(2, ["libs/core/shared.py", "libs/core/b.py"]),
        ]
    )
    assert len(plan.clusters) == 1
    cluster = plan.clusters[0]
    assert cluster.issues == [1, 2]
    assert cluster.pr_mode == "combined"
    assert cluster.executable
    assert "libs/core/shared.py" in cluster.touched_files


def test_hot_file_only_overlap_does_not_merge():
    # Both touch CHANGELOG.md (hot) but no real code overlap -> stay separate,
    # and a serialization note is emitted for the human.
    plan = build_plan(
        [
            _issue(1, ["CHANGELOG.md", "libs/core/a.py"]),
            _issue(2, ["CHANGELOG.md", "libs/core/b.py"]),
        ]
    )
    assert len(plan.clusters) == 2
    assert any("CHANGELOG.md" in n for n in plan.notes)
    for c in plan.clusters:
        assert c.hot_files == ["CHANGELOG.md"]
        assert "CHANGELOG.md" not in c.touched_files


def test_pyproject_and_spec_treated_as_hot():
    plan = build_plan(
        [
            _issue(1, ["libs/connectors/x/pyproject.toml", "libs/connectors/x/a.py"]),
            _issue(2, ["libs/connectors/y/pyproject.toml", "libs/spec/genblaze.json"]),
        ]
    )
    # No shared *code* file -> two clusters despite both touching pyproject/spec.
    assert len(plan.clusters) == 2


def test_dependency_chain_defers_downstream_layer():
    # #2 depends on #1; they touch different files so they are separate
    # components. Only layer-0 (#1) runs now; #2 defers to a re-plan.
    plan = build_plan(
        [
            _issue(1, ["libs/core/a.py"]),
            _issue(2, ["libs/core/b.py"], deps=[1]),
        ]
    )
    clusters = _by_id(plan)
    assert clusters["issues-1"].executable and clusters["issues-1"].layer == 0
    assert not clusters["issues-2"].executable
    assert clusters["issues-2"].layer == 1
    assert clusters["issues-2"].defer_reason == DEFER_AWAITING_PREREQ
    assert clusters["issues-2"].blocked_by == ["issues-1"]


def test_dependency_within_same_cluster_is_not_an_external_edge():
    # Overlapping files put both in one cluster; the intra-cluster dep must not
    # create a self-block that makes the cluster non-executable.
    plan = build_plan(
        [
            _issue(1, ["libs/core/shared.py"]),
            _issue(2, ["libs/core/shared.py"], deps=[1]),
        ]
    )
    assert len(plan.clusters) == 1
    assert plan.clusters[0].executable
    assert not plan.clusters[0].blocked_by


def test_dependency_cycle_is_flagged_and_non_executable():
    plan = build_plan(
        [
            _issue(1, ["libs/core/a.py"], deps=[2]),
            _issue(2, ["libs/core/b.py"], deps=[1]),
        ]
    )
    assert all(not c.executable for c in plan.clusters)
    assert all(c.defer_reason == DEFER_CYCLE for c in plan.clusters)
    assert any("cycle" in n.lower() for n in plan.notes)


def test_oversized_component_serializes_head_and_defers_tail():
    shared = "libs/core/shared.py"
    plan = build_plan(
        [_issue(n, [shared, f"libs/core/f{n}.py"]) for n in (1, 2, 3, 4)],
        Config(max_issues=3),
    )
    clusters = _by_id(plan)
    assert clusters["issues-1"].executable  # head runs now
    tail = clusters["issues-2-3-4"]
    assert not tail.executable
    assert tail.defer_reason == DEFER_OVERSIZED
    assert any("size cap" in n for n in plan.notes)


def test_oversized_by_file_count():
    files_a = [f"libs/core/f{i}.py" for i in range(20)]
    plan = build_plan(
        [
            _issue(1, files_a),
            _issue(2, files_a[:1]),  # shares f0 -> same component, 20 files > cap
        ],
        Config(max_files=12),
    )
    assert any(c.defer_reason == DEFER_OVERSIZED for c in plan.clusters)


def test_skipped_issues_are_excluded_from_clustering():
    plan = build_plan(
        [
            _issue(1, ["libs/core/a.py"]),
            _issue(2, ["libs/core/b.py"], skip="has open PR"),
        ]
    )
    assert [c.issues for c in plan.clusters] == [[1]]
    assert plan.skipped == [{"number": 2, "reason": "has open PR"}]


def test_file_normalization_is_deterministic():
    # Same logical files, different spelling/order/dupes -> identical cluster.
    plan_a = build_plan(
        [
            _issue(1, [" libs/core/a.py ", "libs/core/a.py"]),
            _issue(2, ["libs/core/a.py"]),
        ]
    )
    assert len(plan_a.clusters) == 1
    assert plan_a.clusters[0].touched_files == ["libs/core/a.py"]


def test_dep_on_out_of_scope_issue_is_ignored():
    # #99 already merged/closed (not in scout) -> no spurious block.
    plan = build_plan([_issue(1, ["libs/core/a.py"], deps=[99])])
    assert plan.clusters[0].executable
    assert not plan.clusters[0].blocked_by


def test_plan_serializes_to_json_dict():
    plan = build_plan([_issue(1, ["libs/core/a.py"])])
    d = plan.to_dict()
    assert set(d) == {"clusters", "skipped", "notes"}
    assert d["clusters"][0]["id"] == "issues-1"
