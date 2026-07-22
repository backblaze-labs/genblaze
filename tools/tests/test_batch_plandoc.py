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


def test_roundtrip_write_parse_validate():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    meta = parse_doc(doc)
    # Only #1 is executable; #2 is deferred.
    assert validate(meta, _SHA, live_open_issues=[1, 2]) == [1]


def test_doc_contains_sha_token_and_tables():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    assert _SHA in doc
    assert "Executable now" in doc
    assert "Deferred to a later re-plan" in doc
    assert "has open PR" in doc


def test_tampered_clusters_fail_token_check():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    meta = parse_doc(doc)
    # Attacker/human flips a deferred cluster to executable without re-planning.
    meta["clusters"][1]["executable"] = True
    with pytest.raises(ValidationError, match="token"):
        validate(meta, _SHA, live_open_issues=[1, 2])


def test_advanced_main_is_rejected():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    meta = parse_doc(doc)
    with pytest.raises(ValidationError, match="advanced"):
        validate(meta, live_sha="b" * 40, live_open_issues=[1, 2])


def test_closed_planned_issue_is_rejected():
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    meta = parse_doc(doc)
    # #1 (executable) closed since planning.
    with pytest.raises(ValidationError, match="no longer open"):
        validate(meta, _SHA, live_open_issues=[2])


def test_deferred_issue_closing_does_not_block_execution():
    # #2 is deferred, not executable; its closure is irrelevant to this batch.
    doc = render_doc(_plan(), _SHA, "2026-07-21")
    meta = parse_doc(doc)
    assert validate(meta, _SHA, live_open_issues=[1]) == [1]


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
