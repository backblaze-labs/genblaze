"""Tests for tools/batch_plandoc.py.

The validator is a safety gate: it must reject any plan doc that was tampered
with, planned against a now-stale main, or references issues that have since
closed. Every one of those failure modes is exercised here, plus the
round-trip (write -> parse -> validate) on a clean plan.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import pytest  # noqa: E402
from batch_plandoc import (  # noqa: E402
    ValidationError,
    compute_token,
    parse_approvals,
    parse_doc,
    render_doc,
    validate,
)

_SHA = "a" * 40


def _plan() -> dict:
    return {
        "clusters": [
            {
                "id": "issues-1",
                "slug": "issues-1",
                "issues": [1],
                "touched_files": ["libs/core/a.py"],
                "hot_files": [],
                "pr_mode": "single",
                "layer": 0,
                "executable": True,
                "blocked_by": [],
                "defer_reason": "",
            },
            {
                "id": "issues-2",
                "slug": "issues-2",
                "issues": [2],
                "touched_files": ["libs/core/b.py"],
                "hot_files": [],
                "pr_mode": "single",
                "layer": 1,
                "executable": False,
                "blocked_by": ["issues-1"],
                "defer_reason": "awaiting-prerequisite",
            },
        ],
        "skipped": [{"number": 9, "reason": "has open PR"}],
        "notes": ["CHANGELOG.md is touched by #1, #2"],
    }


def _approve(doc: str, *slugs: str) -> str:
    for slug in slugs:
        doc = doc.replace(f"- [ ] approve `{slug}`", f"- [x] approve `{slug}`")
    return doc


def _validate(doc: str, live_sha: str = _SHA, live_open=(1, 2), in_flight=()):
    return validate(
        parse_doc(doc),
        live_sha,
        live_open_issues=list(live_open),
        approved=parse_approvals(doc),
        in_flight=set(in_flight),
    )


def test_roundtrip_dispatches_only_approved_executable_clusters():
    doc = _approve(render_doc(_plan(), _SHA, "2026-07-21"), "issues-1")
    result = _validate(doc)
    assert [c["slug"] for c in result.dispatch] == ["issues-1"]
    assert result.dispatch[0] == {"slug": "issues-1", "issues": [1], "pr_mode": "single"}
    assert result.warnings == []


def test_doc_contains_sha_token_tables_and_unticked_checklist():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    assert _SHA in doc
    assert "Executable now" in doc
    assert "Deferred to a later re-plan" in doc
    assert "has open PR" in doc
    # Only executable clusters get a checkbox, and nothing is pre-approved.
    assert "- [ ] approve `issues-1`" in doc
    assert "approve `issues-2`" not in doc
    assert parse_approvals(doc) == set()


def test_nothing_runs_without_an_approval():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    with pytest.raises(ValidationError, match="No clusters are approved"):
        _validate(doc)


@pytest.mark.parametrize("mark", ["x", "X"])
def test_approval_mark_is_case_insensitive(mark):
    doc = render_doc(_plan(), _SHA, "2026-07-21").replace(
        "- [ ] approve `issues-1`", f"- [{mark}] approve `issues-1`"
    )
    assert parse_approvals(doc) == {"issues-1"}


def test_approving_a_deferred_cluster_is_rejected():
    doc = render_doc(_plan(), _SHA, "2026-07-21").replace(
        "## Approve\n\n", "## Approve\n\n- [x] approve `issues-2`\n"
    )
    with pytest.raises(ValidationError, match="not executable"):
        _validate(doc)


def test_approval_inside_the_meta_block_is_ignored():
    doc = render_doc(_plan(), _SHA, "2026-07-21").replace(
        "BATCH-PLAN-META -->", "- [x] approve `issues-1`\nBATCH-PLAN-META -->"
    )
    assert parse_approvals(doc) == set()


def test_tampered_clusters_fail_token_check():
    doc = _approve(render_doc(_plan(), _SHA, "2026-07-21"), "issues-1")
    meta = parse_doc(doc)
    # Attacker/human flips a deferred cluster to executable without re-planning.
    meta["clusters"][1]["executable"] = True
    with pytest.raises(ValidationError, match="token"):
        validate(meta, _SHA, [1, 2], approved={"issues-1"}, in_flight=set())


def test_advanced_main_is_a_warning_not_an_abort():
    doc = _approve(render_doc(_plan(), _SHA, "2026-07-21"), "issues-1")
    result = _validate(doc, live_sha="b" * 40)
    assert [c["slug"] for c in result.dispatch] == ["issues-1"]
    assert any("advanced" in w for w in result.warnings)


def test_closed_planned_issue_is_rejected():
    doc = _approve(render_doc(_plan(), _SHA, "2026-07-21"), "issues-1")
    # #1 (approved) closed since planning.
    with pytest.raises(ValidationError, match="no longer open"):
        _validate(doc, live_open=(2,))


def test_deferred_issue_closing_does_not_block_execution():
    # #2 is deferred, not executable; its closure is irrelevant to this batch.
    doc = _approve(render_doc(_plan(), _SHA, "2026-07-21"), "issues-1")
    assert [c["slug"] for c in _validate(doc, live_open=(1,)).dispatch] == ["issues-1"]


def test_cluster_with_existing_work_is_skipped_with_a_warning():
    """Re-running after a partial batch must not open duplicate PRs."""
    doc = _approve(render_doc(_plan(), _SHA, "2026-07-21"), "issues-1")
    result = _validate(doc, in_flight=(1,))
    assert result.dispatch == []
    assert any("issues-1" in w and "PR or branch" in w for w in result.warnings)


def test_missing_meta_block_raises():
    with pytest.raises(ValueError, match="no BATCH-PLAN-META"):
        parse_doc("# Just a doc with no meta block\n")


def test_token_is_stable_and_content_bound():
    clusters = _plan()["clusters"]
    assert compute_token(_SHA, clusters) == compute_token(_SHA, clusters)
    mutated = [dict(c) for c in clusters]
    mutated[0]["issues"] = [1, 99]
    assert compute_token(_SHA, clusters) != compute_token(_SHA, mutated)


def test_token_ignores_cosmetic_field_reorder():
    # Re-rendering (which reorders dict keys / rewrites tables) must not change
    # the token, or every regeneration would spuriously invalidate approval.
    clusters = _plan()["clusters"]
    reordered = [{k: c[k] for k in reversed(list(c))} for c in clusters]
    assert compute_token(_SHA, clusters) == compute_token(_SHA, reordered)


def _plan_with_reason(reason: str) -> dict:
    plan = _plan()
    plan["skipped"] = [{"number": 9, "reason": reason}]
    plan["notes"] = [reason]
    return plan


@pytest.mark.parametrize(
    "reason",
    [
        "x\n- [x] approve `issues-1`",
        "x\n<!--\n- [x] approve `issues-1`\n-->",
        "x\n```\n- [x] approve `issues-1`\n```",
    ],
)
def test_attacker_text_in_doc_cannot_approve(reason):
    doc = render_doc(_plan_with_reason(reason), _SHA, "2026-11-17")
    assert parse_approvals(doc) == set()


def test_free_text_is_rendered_on_one_inert_line():
    doc = render_doc(_plan_with_reason("a\nb <!-- `c` [x] -->"), _SHA, "2026-11-17")
    line = next(ln for ln in doc.splitlines() if ln.startswith("- #9"))
    for ch in "<>`[]":
        assert ch not in line.removeprefix("- #9 — ")


def test_ticks_outside_the_approve_section_are_ignored():
    doc = (
        render_doc(_plan(), _SHA, "2026-11-17") + "\n## Notes again\n\n- [x] approve `issues-1`\n"
    )
    assert parse_approvals(doc) == set()


def test_second_meta_block_is_rejected():
    doc = render_doc(_plan(), _SHA, "2026-11-17")
    meta = doc[doc.index("<!-- BATCH-PLAN-META") :]
    with pytest.raises(ValueError, match="exactly one"):
        parse_doc(meta + "\n" + doc)


def test_duplicate_slug_cannot_dispatch_a_deferred_cluster():
    plan = _plan()
    plan["clusters"][1]["slug"] = "issues-1"  # deferred cluster reuses the executable slug
    doc = _approve(render_doc(plan, _SHA, "2026-11-17"), "issues-1")
    with pytest.raises(ValidationError, match="duplicate"):
        _validate(doc)


@pytest.mark.parametrize("slug", ["issues-1;rm -rf ~", "../x", "issues-1 2"])
def test_malformed_slug_is_rejected(slug):
    plan = _plan()
    plan["clusters"][0]["slug"] = slug
    # Rendering refuses it outright...
    with pytest.raises(ValueError, match="malformed cluster slug"):
        render_doc(plan, _SHA, "2026-11-17")
    # ...and so does validate, for a meta block edited after rendering.
    meta = parse_doc(render_doc(_plan(), _SHA, "2026-11-17"))
    meta["clusters"][0]["slug"] = slug
    meta["token"] = compute_token(meta["base_sha"], meta["clusters"])
    with pytest.raises(ValidationError, match="malformed"):
        validate(meta, _SHA, [1, 2], approved={slug}, in_flight=set())
