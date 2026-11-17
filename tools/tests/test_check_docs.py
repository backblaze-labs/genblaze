"""Tests for tools/check_docs.py (Repo Docs Standard lint).

Pure checks are tested on in-memory text; the git-backed pieces (changed
files, added lines, commit messages) run against a throwaway repo.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_docs as cd  # noqa: E402

NOW = datetime(2026, 11, 16, tzinfo=UTC)

GOOD_FM = """---
type: Library
title: Pipeline
description: Fluent pipeline API.
status: stable
stale_after: 2027-05-01T00:00:00Z
owner: "@jeronimodeleon"
---

# Pipeline
"""


def _levels(findings: list[cd.Finding]) -> list[str]:
    return [f.level for f in findings]


def _messages(findings: list[cd.Finding]) -> str:
    return "\n".join(f.message for f in findings)


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_valid():
    meta, error = cd.parse_frontmatter(GOOD_FM)
    assert error is None
    assert meta is not None and meta["type"] == "Library"


def test_parse_frontmatter_absent_returns_none_without_error():
    meta, error = cd.parse_frontmatter("<!-- last_verified: 2026-01-01 -->\n# Doc\n")
    assert meta is None and error is None


def test_parse_frontmatter_tolerates_bom_and_crlf():
    text = "﻿" + GOOD_FM.replace("\n", "\r\n")
    meta, error = cd.parse_frontmatter(text)
    assert error is None and meta is not None and meta["status"] == "stable"


def test_parse_frontmatter_unclosed_fence_is_error_not_crash():
    meta, error = cd.parse_frontmatter("---\n\n# A doc that opens with a rule\n")
    assert meta is None and error is not None and "closing" in error


def test_parse_frontmatter_malformed_yaml_is_error():
    meta, error = cd.parse_frontmatter("---\ntype: [Library\n---\n")
    assert meta is None and error is not None


def test_parse_frontmatter_unquoted_at_owner_hints_quoting():
    meta, error = cd.parse_frontmatter("---\nowner: @jeronimodeleon\n---\n")
    assert meta is None and error is not None and "quote" in error


def test_parse_frontmatter_non_mapping_is_error():
    meta, error = cd.parse_frontmatter("---\n- a\n- b\n---\n")
    assert meta is None and error is not None


# ---------------------------------------------------------------------------
# check_frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_valid_doc_has_no_findings():
    assert cd.check_frontmatter("docs/features/pipeline.md", GOOD_FM, changed=True, now=NOW) == []


def test_frontmatter_missing_on_changed_doc_is_error():
    findings = cd.check_frontmatter("docs/features/x.md", "# X\n", changed=True, now=NOW)
    assert _levels(findings) == ["error"]


def test_frontmatter_missing_on_unchanged_doc_is_silent():
    # Legacy docs are exempt until touched; run() reports them as one summary line.
    assert cd.check_frontmatter("docs/features/x.md", "# X\n", changed=False, now=NOW) == []


@pytest.mark.parametrize("key", ["type", "owner", "status", "stale_after"])
def test_frontmatter_missing_required_key_is_error(key):
    text = "\n".join(line for line in GOOD_FM.splitlines() if not line.startswith(f"{key}:"))
    findings = cd.check_frontmatter("docs/a.md", text, changed=True, now=NOW)
    assert "error" in _levels(findings) and key in _messages(findings)


def test_frontmatter_unknown_type_is_error():
    text = GOOD_FM.replace("type: Library", "type: Feature")
    findings = cd.check_frontmatter("docs/a.md", text, changed=False, now=NOW)
    assert _levels(findings) == ["error"] and "Feature" in _messages(findings)


def test_frontmatter_unknown_status_is_error():
    text = GOOD_FM.replace("status: stable", "status: wip")
    assert _levels(cd.check_frontmatter("docs/a.md", text, changed=False, now=NOW)) == ["error"]


def test_frontmatter_date_only_stale_after_is_error():
    text = GOOD_FM.replace("2027-05-01T00:00:00Z", "2027-05-01")
    findings = cd.check_frontmatter("docs/a.md", text, changed=False, now=NOW)
    assert _levels(findings) == ["error"] and "UTC offset" in _messages(findings)


def test_frontmatter_naive_datetime_stale_after_is_error():
    text = GOOD_FM.replace("2027-05-01T00:00:00Z", "2027-05-01T00:00:00")
    assert _levels(cd.check_frontmatter("docs/a.md", text, changed=False, now=NOW)) == ["error"]


def test_frontmatter_quoted_iso_string_stale_after_is_accepted():
    text = GOOD_FM.replace("2027-05-01T00:00:00Z", '"2027-05-01T00:00:00Z"')
    assert cd.check_frontmatter("docs/a.md", text, changed=False, now=NOW) == []


def test_frontmatter_past_stale_after_is_warning_only():
    text = GOOD_FM.replace("2027-05-01T00:00:00Z", "2026-01-01T00:00:00Z")
    findings = cd.check_frontmatter("docs/a.md", text, changed=True, now=NOW)
    assert _levels(findings) == ["warning"] and "stale" in _messages(findings)


def test_frontmatter_stale_but_deprecated_is_silent():
    text = GOOD_FM.replace("2027-05-01T00:00:00Z", "2026-01-01T00:00:00Z").replace(
        "status: stable", "status: deprecated"
    )
    assert cd.check_frontmatter("docs/a.md", text, changed=True, now=NOW) == []


@pytest.mark.parametrize(
    "rel",
    [
        "docs/index.md",
        "docs/features/index.md",
        "docs/log.md",
        "docs/features/_template.md",
        "docs/reference/model-matrix.md",
        "docs/exec-plans/active/batch-plan-2026-11-16.md",
        "docs/exec-plans/active/maintenance-report-2026-11-16.md",
    ],
)
def test_frontmatter_exempt_paths(rel):
    assert cd.is_frontmatter_exempt(rel)


def test_frontmatter_not_exempt_for_normal_doc():
    assert not cd.is_frontmatter_exempt("docs/features/pipeline.md")


# ---------------------------------------------------------------------------
# check_links
# ---------------------------------------------------------------------------


@pytest.fixture
def link_root(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "features").mkdir(parents=True)
    (tmp_path / "docs" / "features" / "pipeline.md").write_text("# P\n")
    (tmp_path / "ARCHITECTURE.md").write_text("# A\n")
    return tmp_path


def test_links_resolve_relative_to_file(link_root):
    text = "[p](features/pipeline.md) [a](../ARCHITECTURE.md#layout) [dir](features/)\n"
    assert cd.check_links(link_root, "docs/index.md", text, changed=True) == []


def test_links_missing_target_in_changed_file_is_error_with_line(link_root):
    text = "intro\n\nsee [gone](features/gone.md)\n"
    findings = cd.check_links(link_root, "docs/index.md", text, changed=True)
    assert _levels(findings) == ["error"] and findings[0].line == 3


def test_links_broken_only_on_preexisting_line_of_changed_file_is_warning(link_root):
    # Editing one line of a long file must not inherit its old broken links.
    text = "[new](features/pipeline.md)\n\n[old](features/gone.md)\n[added](nope.md)\n"
    findings = cd.check_links(link_root, "docs/index.md", text, changed=True, added={1, 4})
    assert [(f.level, f.line) for f in findings] == [("warning", 3), ("error", 4)]


def test_links_missing_target_in_unchanged_file_is_warning(link_root):
    findings = cd.check_links(link_root, "docs/index.md", "[gone](gone.md)\n", changed=False)
    assert _levels(findings) == ["warning"]


def test_links_skip_external_anchor_and_mailto(link_root):
    text = "[x](https://example.com/a.md) [y](#section) [z](mailto:a@b.c) [w](http://x.y)\n"
    assert cd.check_links(link_root, "docs/index.md", text, changed=True) == []


def test_links_inside_fenced_and_inline_code_are_ignored(link_root):
    text = "```md\n[gone](gone.md)\n```\n`[also](gone.md)`\n~~~\n[x](nope.md)\n~~~\n"
    assert cd.check_links(link_root, "docs/index.md", text, changed=True) == []


def test_links_with_title_and_angle_brackets(link_root):
    text = '[p](features/pipeline.md "Pipeline") [q](<features/pipeline.md>)\n'
    assert cd.check_links(link_root, "docs/index.md", text, changed=True) == []


def test_links_repo_root_absolute(link_root):
    assert cd.check_links(link_root, "docs/index.md", "[a](/ARCHITECTURE.md)\n", True) == []


# ---------------------------------------------------------------------------
# check_commands
# ---------------------------------------------------------------------------

MAKEFILE = (
    ".PHONY: test lint\n\ntest:\n\tpytest\n\nlint: deptry\n\truff check\n\n"
    "ts-types-check: ts-types\n"
)


def test_commands_known_targets_pass():
    text = "Run `make test`, `make lint`, and `make ts-types-check`.\n"
    assert cd.check_commands("AGENTS.md", text, MAKEFILE) == []


def test_commands_unknown_target_is_error_with_line():
    findings = cd.check_commands("AGENTS.md", "ok\nRun `make verifyy` first\n", MAKEFILE)
    assert _levels(findings) == ["error"] and findings[0].line == 2
    assert "verifyy" in findings[0].message


def test_commands_with_variables_use_target_name():
    assert cd.check_commands("AGENTS.md", "`make test VERSION=1.0`\n", MAKEFILE) == []


def test_commands_ignore_non_make_and_prose():
    text = "`/test-package core` and `cd libs/core && pytest` and make sure things work\n"
    assert cd.check_commands("AGENTS.md", text, MAKEFILE) == []


# ---------------------------------------------------------------------------
# scan_leaks
# ---------------------------------------------------------------------------

KEY_HASHES = frozenset({cd.sha256_hex("PROJ")})
HOST_HASHES = frozenset({cd.sha256_hex("wiki.internal.example")})


def _scan(text: str) -> list[cd.Finding]:
    return cd.scan_leaks(text, "src.md", 1, KEY_HASHES, HOST_HASHES)


def test_leak_hashed_jira_key_detected():
    findings = _scan("fixes PROJ-123 in the sink")
    assert _levels(findings) == ["error"]
    # The finding never echoes the matched secret-ish token.
    assert "PROJ" not in findings[0].message


def test_leak_hashed_host_and_subdomain_detected():
    assert _levels(_scan("see https://wiki.internal.example/page/1")) == ["error"]
    assert _levels(_scan("see https://a.wiki.internal.example/x")) == ["error"]


def test_leak_atlassian_host_detected_in_plaintext():
    # Built by concatenation so this file doesn't trip the guard it tests.
    assert _levels(_scan("https://acme.atlassian" + ".net/browse/X-1")) == ["error"]


@pytest.mark.parametrize(
    "text",
    [
        "UTF-8 encoding",
        "SHA-256 digest",
        "CVE-2026-1234",
        "GHSA-abcd-1234-wxyz",
        "PEP-621 metadata",
        "PROJECT-123 is a different key",
        "lowercase proj-123",
        "https://github.com/backblaze-labs/genblaze/issues/1",
    ],
)
def test_leak_false_positives_not_matched(text):
    assert _scan(text) == []


def test_leak_line_numbers_offset():
    findings = cd.scan_leaks("a\nb PROJ-9\n", "x", 10, KEY_HASHES, HOST_HASHES)
    assert findings[0].line == 11


# ---------------------------------------------------------------------------
# check_required
# ---------------------------------------------------------------------------


def test_required_files_missing_are_errors(tmp_path):
    findings = cd.check_required(tmp_path)
    missing = {f.path for f in findings}
    assert missing == {"AGENTS.md", "CLAUDE.md", "docs/index.md"}
    assert set(_levels(findings)) == {"error"}


def test_claude_md_must_import_agents_md(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "AGENTS.md").write_text("# A\n")
    (tmp_path / "docs" / "index.md").write_text("# I\n")
    (tmp_path / "CLAUDE.md").write_text("Follow AGENTS.md\n")
    findings = cd.check_required(tmp_path)
    assert _levels(findings) == ["error"] and "@AGENTS.md" in findings[0].message
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n")
    assert cd.check_required(tmp_path) == []


# ---------------------------------------------------------------------------
# git-backed: changed files, added lines, commit messages, run(), main()
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs" / "features").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("Run `make test`.\n")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n")
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    (tmp_path / "docs" / "index.md").write_text("- [old](features/old.md)\n")
    (tmp_path / "docs" / "features" / "old.md").write_text("# Old legacy doc\n")
    (tmp_path / "docs" / "features" / "moved.md").write_text("# To be renamed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-qb", "feature")
    return tmp_path


def test_changed_files_include_untracked_and_renames_but_not_deletes(repo):
    (repo / "docs" / "features" / "new.md").write_text("# new\n")
    _git(repo, "mv", "docs/features/moved.md", "docs/features/renamed.md")
    (repo / "docs" / "features" / "old.md").unlink()
    changed = cd.changed_files(repo, "main")
    assert "docs/features/new.md" in changed
    assert "docs/features/renamed.md" in changed
    assert "docs/features/old.md" not in changed


def test_run_legacy_unchanged_docs_produce_single_summary_warning(repo):
    findings = cd.run(repo, base="main", now=NOW)
    assert _levels(findings) == ["warning"]
    assert "2 docs" in findings[0].message


def test_run_new_doc_without_frontmatter_is_error(repo):
    (repo / "docs" / "features" / "new.md").write_text("# new\n")
    findings = cd.run(repo, base="main", now=NOW)
    assert any(f.level == "error" and f.path == "docs/features/new.md" for f in findings)


def test_run_old_broken_link_in_edited_file_warns_new_one_errors(repo):
    _git(repo, "checkout", "-q", "main")
    (repo / "NOTES.md").write_text("[old](gone.md)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "notes")
    _git(repo, "checkout", "-qB", "feature")
    (repo / "NOTES.md").write_text("[old](gone.md)\nnew line\n")
    links = [f for f in cd.run(repo, base="main", now=NOW) if "broken link" in f.message]
    assert [f.level for f in links] == ["warning"]

    (repo / "NOTES.md").write_text("[old](gone.md)\n[new](missing.md)\n")
    links = [f for f in cd.run(repo, base="main", now=NOW) if "broken link" in f.message]
    assert sorted((f.level, f.line) for f in links) == [("error", 2), ("warning", 1)]


def test_run_scans_added_lines_and_commit_messages_for_leaks(repo, monkeypatch):
    monkeypatch.setattr(cd, "JIRA_KEY_HASHES", KEY_HASHES)
    (repo / "notes.txt").write_text("tracking PROJ-42\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip PROJ-43")
    leaks = [f for f in cd.run(repo, base="main", now=NOW) if "internal" in f.message]
    assert {f.path for f in leaks} == {"notes.txt", "commit message"}


def test_run_scans_pr_title_and_body(repo, monkeypatch):
    monkeypatch.setattr(cd, "JIRA_KEY_HASHES", KEY_HASHES)
    findings = cd.run(repo, base="main", now=NOW, pr_texts={"PR title": "PROJ-1 fix"})
    assert any(f.path == "PR title" and f.level == "error" for f in findings)


def test_run_without_base_skips_leak_scan_and_treats_nothing_as_changed(repo):
    (repo / "docs" / "features" / "new.md").write_text("# new\n")
    assert set(_levels(cd.run(repo, base=None, now=NOW))) == {"warning"}


def test_main_exit_codes_and_annotations(repo, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cd.main(["--root", str(repo), "--base", "main"]) == 0
    assert "::warning" in capsys.readouterr().out
    (repo / "docs" / "features" / "new.md").write_text("# new\n")
    assert cd.main(["--root", str(repo), "--base", "main"]) == 1
    assert "::error file=docs/features/new.md" in capsys.readouterr().out


def test_main_freshness_mode_never_fails(repo):
    (repo / "docs" / "features" / "new.md").write_text("# new\n")
    assert cd.main(["--root", str(repo), "--freshness"]) == 0


def test_main_hash_helper_prints_digest(capsys):
    assert cd.main(["--hash", "PROJ"]) == 0
    assert capsys.readouterr().out.strip() == cd.sha256_hex("PROJ")
