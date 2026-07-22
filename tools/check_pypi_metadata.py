#!/usr/bin/env python3
"""CI gate: every published genblaze package has consistent PyPI metadata.

Walks ``libs/**/pyproject.toml`` plus ``cli/pyproject.toml`` and asserts
every published Python package has the metadata fields users expect to
see on PyPI:

* ``description`` — single sentence, ≤ 200 chars
* ``readme`` — set (safe per-package README file path, not just root link)
* ``authors`` — populated
* ``license`` — set (PEP 639 SPDX expression, e.g. ``license = "MIT"``); no
  redundant ``License ::`` classifier alongside it (PEP 639 says classifiers
  SHOULD NOT be used when a license expression is present, and
  setuptools >= 77 errors on the combination — see CHANGELOG #60)
* ``requires-python`` — ``>=3.11`` (matches AGENTS.md invariant)
* ``classifiers`` — Python versions (3.11/3.12/3.13), Topic, Development
  Status
* ``project.urls`` — Homepage, Documentation, Repository, Issues
* ``keywords`` — non-empty
* ``readme`` Markdown links — absolute URLs only for files rendered on PyPI

Run from the repo root:

    python tools/check_pypi_metadata.py        # report-only
    python tools/check_pypi_metadata.py --strict   # exit 1 on any miss

Designed to fail loudly in CI so a release-prep PR can't add a new
package whose PyPI page renders empty.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

# Required classifier prefixes — at least one classifier in each group
# must be present. License is intentionally excluded: PEP 639's
# `license = "MIT"` SPDX expression is the single source of truth (see the
# `_LICENSE_CLASSIFIER_PREFIX` check below, which asserts the classifier is
# *absent*, not present).
_REQUIRED_CLASSIFIER_GROUPS: dict[str, list[str]] = {
    "python_versions": ["Programming Language :: Python :: 3.1"],
    "topic": ["Topic :: "],
    "dev_status": ["Development Status :: "],
}

# PEP 639: a `License ::` trove classifier is redundant — and, on
# setuptools >= 77, an error — once a `license` SPDX expression is set.
_LICENSE_CLASSIFIER_PREFIX = "License :: "

# Required project_urls keys (case-sensitive — matches PyPI rendering).
_REQUIRED_PROJECT_URLS = ("Homepage", "Documentation", "Repository", "Issues")

# Description must fit comfortably in PyPI's search-result preview.
_DESCRIPTION_MAX_CHARS = 200  # Plan suggested 120 but real packages run a bit longer
_README_MAX_BYTES = 1_000_000

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_REFERENCE_LINK_RE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*([^\s]+)")
_ALLOWED_MARKDOWN_URL_SCHEMES = {"http", "https", "mailto"}


def _markdown_target_issue_kind(target: str) -> str | None:
    """Return the PyPI-rendering issue kind for ``target`` if it is invalid."""
    clean_target = target.strip().strip("<>")
    if not clean_target or clean_target.startswith("#"):
        return None

    scheme = urlparse(clean_target).scheme
    if scheme in _ALLOWED_MARKDOWN_URL_SCHEMES:
        return None
    if scheme:
        return "unsupported markdown link scheme"
    return "relative markdown link"


def _iter_inline_markdown_targets(line: str) -> list[str]:
    """Return inline Markdown link targets from ``line``.

    This intentionally implements only the syntax shape this gate needs:
    balanced link labels followed by ``(...)`` destinations. It is stricter
    than a full Markdown parser, but catches nested-bracket labels that simple
    regular expressions miss.
    """
    targets: list[str] = []
    index = 0
    while index < len(line):
        label_start = line.find("[", index)
        if label_start == -1:
            break

        cursor = label_start + 1
        label_depth = 1
        while cursor < len(line) and label_depth:
            char = line[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "[":
                label_depth += 1
            elif char == "]":
                label_depth -= 1
            cursor += 1

        if label_depth:
            index = label_start + 1
            continue

        while cursor < len(line) and line[cursor] in " \t":
            cursor += 1
        if cursor >= len(line) or line[cursor] != "(":
            index = cursor
            continue

        cursor += 1
        while cursor < len(line) and line[cursor] in " \t":
            cursor += 1

        if cursor < len(line) and line[cursor] == "<":
            target_end = line.find(">", cursor + 1)
            if target_end != -1:
                targets.append(line[cursor : target_end + 1])
                index = _link_end_index(line, target_end + 1) + 1
                continue
            index = cursor + 1
            continue

        target_start = cursor
        paren_depth = 0
        while cursor < len(line):
            char = line[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "(":
                paren_depth += 1
            elif char == ")":
                if paren_depth == 0:
                    break
                paren_depth -= 1
            elif char in " \t":
                break
            cursor += 1

        if cursor > target_start:
            targets.append(line[target_start:cursor])
            index = _link_end_index(line, cursor) + 1
        else:
            index = cursor + 1

    return targets


def _link_end_index(line: str, cursor: int) -> int:
    """Return the index of the closing ``)`` for an inline Markdown link."""
    quote: str | None = None
    paren_depth = 0
    while cursor < len(line):
        char = line[cursor]
        if char == "\\":
            cursor += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            if paren_depth == 0:
                return cursor
            paren_depth -= 1
        cursor += 1
    return cursor


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    """Return stable identity fields for detecting read-after-validate swaps."""
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat.S_IFMT(stat_result.st_mode),
        stat_result.st_size,
    )


def _iter_markdown_link_targets(
    readme_path: Path, expected_stat: os.stat_result
) -> list[tuple[int, str]]:
    """Return ``(line_number, target)`` pairs for Markdown links in a file."""
    targets: list[tuple[int, str]] = []
    fence_marker: str | None = None

    with readme_path.open(encoding="utf-8") as readme_handle:
        opened_stat = os.fstat(readme_handle.fileno())
        if _stat_identity(opened_stat) != _stat_identity(expected_stat):
            raise OSError("readme file changed after validation")

        for line_number, line in enumerate(readme_handle, start=1):
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if fence_marker is None:
                    fence_marker = marker
                    continue
                if marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                    fence_marker = None
                    continue
            if fence_marker is not None:
                continue

            for match in _REFERENCE_LINK_RE.finditer(line):
                targets.append((line_number, match.group(1)))
            for target in _iter_inline_markdown_targets(line):
                targets.append((line_number, target))

    return targets


def _validated_readme_path(
    path: Path, readme_file: str
) -> tuple[Path | None, os.stat_result | None, list[str]]:
    """Validate a package README path before opening it."""
    declared = Path(readme_file)
    if declared.is_absolute():
        return None, None, [f"readme path must be relative: {readme_file}"]
    if any(part == ".." for part in declared.parts):
        return None, None, [f"readme path must stay within package: {readme_file}"]

    current = path.parent
    for part in declared.parts:
        current = current / part
        try:
            stat_result = current.lstat()
        except FileNotFoundError:
            return None, None, [f"readme file not found: {readme_file}"]
        except OSError as exc:
            return None, None, [f"readme file cannot be inspected: {readme_file}: {exc}"]

        if stat.S_ISLNK(stat_result.st_mode):
            return None, None, [f"readme path must not contain symlinks: {readme_file}"]

    if not stat.S_ISREG(stat_result.st_mode):
        return None, None, [f"readme path is not a regular file: {readme_file}"]
    if stat_result.st_size > _README_MAX_BYTES:
        return (
            None,
            None,
            [
                f"readme file too large: {readme_file} "
                f"({stat_result.st_size} bytes > {_README_MAX_BYTES})"
            ],
        )

    return current, stat_result, []


def _check_readme_links(path: Path, readme: object) -> list[str]:
    """Return PyPI-rendering issues for the package README."""
    readme_file: str | None = None
    if isinstance(readme, str):
        readme_file = readme
    elif isinstance(readme, dict):
        file_value = readme.get("file")
        if isinstance(file_value, str):
            readme_file = file_value
        else:
            return ["readme must reference a file path"]
    else:
        return ["readme must reference a file path"]

    if not readme_file:
        return ["readme must reference a file path"]

    readme_path, stat_result, issues = _validated_readme_path(path, readme_file)
    if issues or readme_path is None or stat_result is None:
        return issues
    if readme_path.suffix.lower() != ".md":
        return []

    try:
        targets = _iter_markdown_link_targets(readme_path, stat_result)
    except (OSError, UnicodeError) as exc:
        return [f"readme file cannot be read: {readme_file}: {exc}"]

    link_issues: list[str] = []
    for line_number, target in targets:
        issue_kind = _markdown_target_issue_kind(target)
        if issue_kind:
            link_issues.append(f"{issue_kind} in {readme_file}:{line_number} -> {target}")
    return link_issues


def _check_package(path: Path) -> list[str]:
    """Return a list of human-readable issues found in ``path`` (empty
    list = clean)."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    project = raw.get("project")
    if not project:
        return []  # not a published package (e.g. workspace-root pyproject)
    issues: list[str] = []

    name = project.get("name", "<missing>")

    # description
    desc = project.get("description")
    if not desc or not desc.strip():
        issues.append("missing `description`")
    elif len(desc) > _DESCRIPTION_MAX_CHARS:
        issues.append(f"description {len(desc)} chars (>{_DESCRIPTION_MAX_CHARS})")

    # readme
    readme = project.get("readme")
    if not readme:
        issues.append("missing `readme`")
    else:
        issues.extend(_check_readme_links(path, readme))

    # authors
    authors = project.get("authors")
    if not authors:
        issues.append("missing `authors`")
    else:
        for author in authors:
            if not author.get("name"):
                issues.append(f"author missing `name` field: {author!r}")

    # license
    license_field = project.get("license")
    if not license_field:
        issues.append("missing `license`")
    if any(c.startswith(_LICENSE_CLASSIFIER_PREFIX) for c in project.get("classifiers", [])):
        issues.append(
            "redundant `License ::` classifier alongside `license` SPDX expression (PEP 639)"
        )

    # requires-python
    rp = project.get("requires-python")
    if not rp:
        issues.append("missing `requires-python`")
    elif "3.11" not in rp:
        issues.append(f"`requires-python={rp!r}` should pin >=3.11")

    # classifiers
    classifiers = project.get("classifiers", [])
    for group_name, prefixes in _REQUIRED_CLASSIFIER_GROUPS.items():
        if not any(any(c.startswith(p) for p in prefixes) for c in classifiers):
            issues.append(f"missing `classifiers` group: {group_name}")

    # project_urls
    project_urls = project.get("urls", {}) or project.get("project_urls", {}) or {}
    for required_key in _REQUIRED_PROJECT_URLS:
        if required_key not in project_urls:
            issues.append(f"missing `project.urls.{required_key}`")

    # keywords
    keywords = project.get("keywords")
    if not keywords:
        issues.append("missing `keywords`")

    return [f"{name}: {issue}" for issue in issues]


def _find_pyprojects(repo_root: Path) -> list[Path]:
    """Locate every published-package pyproject.toml.

    Skips: the repo-root pyproject (workspace config, not a package),
    any non-python pyproject (none currently), and editable test
    fixtures.
    """
    paths: list[Path] = []
    for candidate in repo_root.glob("libs/**/pyproject.toml"):
        if "node_modules" in candidate.parts:
            continue
        paths.append(candidate)
    cli_pyproject = repo_root / "cli" / "pyproject.toml"
    if cli_pyproject.exists():
        paths.append(cli_pyproject)
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any package has issues (CI mode).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root (default: parent of tools/).",
    )
    args = parser.parse_args()

    paths = _find_pyprojects(args.repo_root)
    if not paths:
        print("No pyproject.toml files found.", file=sys.stderr)
        return 1

    all_issues: list[tuple[Path, list[str]]] = []
    for path in paths:
        issues = _check_package(path)
        all_issues.append((path, issues))

    total_issues = sum(len(issues) for _, issues in all_issues)
    clean_count = sum(1 for _, issues in all_issues if not issues)
    print(
        f"Audited {len(paths)} pyproject.toml — {clean_count} clean, "
        f"{len(paths) - clean_count} with issues, {total_issues} issues total."
    )
    print()
    for path, issues in all_issues:
        rel = path.relative_to(args.repo_root)
        if not issues:
            print(f"  ✓ {rel}")
        else:
            print(f"  ✗ {rel}")
            for issue in issues:
                print(f"      {issue}")

    if args.strict and total_issues > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
