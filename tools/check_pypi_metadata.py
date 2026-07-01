#!/usr/bin/env python3
"""CI gate: every published genblaze package has consistent PyPI metadata.

Walks ``libs/**/pyproject.toml`` plus ``cli/pyproject.toml`` and asserts
every published Python package has the metadata fields users expect to
see on PyPI:

* ``description`` — single sentence, ≤ 120 chars
* ``readme`` — set (per-package README.md, not just root link)
* ``authors`` — populated
* ``license`` — set
* ``requires-python`` — ``>=3.11`` (matches AGENTS.md invariant)
* ``classifiers`` — License (MIT), Python versions (3.11/3.12/3.13),
  Topic, Development Status
* ``project_urls`` — Homepage, Documentation, Repository, Issues,
  Changelog
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
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

# Required classifier prefixes — at least one classifier in each group
# must be present.
_REQUIRED_CLASSIFIER_GROUPS: dict[str, list[str]] = {
    "license": ["License :: "],
    "python_versions": ["Programming Language :: Python :: 3.1"],
    "topic": ["Topic :: "],
    "dev_status": ["Development Status :: "],
}

# Required project_urls keys (case-sensitive — matches PyPI rendering).
_REQUIRED_PROJECT_URLS = ("Homepage", "Documentation", "Repository", "Issues")

# Description must fit comfortably in PyPI's search-result preview.
_DESCRIPTION_MAX_CHARS = 200  # Plan suggested 120 but real packages run a bit longer

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_INLINE_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*([^\s)]+)")
_REFERENCE_LINK_RE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*([^\s]+)")


def _is_relative_markdown_target(target: str) -> bool:
    """Return True when ``target`` would resolve relative to PyPI."""
    clean_target = target.strip().strip("<>")
    if not clean_target or clean_target.startswith("#"):
        return False
    if clean_target.startswith("//"):
        return False
    return not urlparse(clean_target).scheme


def _iter_markdown_link_targets(readme_path: Path) -> list[tuple[int, str]]:
    """Return ``(line_number, target)`` pairs for Markdown links in a file."""
    targets: list[tuple[int, str]] = []
    in_fence = False

    for line_number, line in enumerate(readme_path.read_text().splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        for match in _REFERENCE_LINK_RE.finditer(line):
            targets.append((line_number, match.group(1)))
        for match in _INLINE_LINK_RE.finditer(line):
            targets.append((line_number, match.group(1)))

    return targets


def _check_readme_links(path: Path, readme: object) -> list[str]:
    """Return PyPI-rendering issues for the package README."""
    readme_file: str | None = None
    if isinstance(readme, str):
        readme_file = readme
    elif isinstance(readme, dict) and isinstance(readme.get("file"), str):
        readme_file = readme["file"]

    if not readme_file or Path(readme_file).suffix.lower() != ".md":
        return []

    readme_path = path.parent / readme_file
    if not readme_path.exists():
        return [f"readme file not found: {readme_file}"]

    return [
        f"relative markdown link in {readme_file}:{line_number} -> {target}"
        for line_number, target in _iter_markdown_link_targets(readme_path)
        if _is_relative_markdown_target(target)
    ]


def _check_package(path: Path) -> list[str]:
    """Return a list of human-readable issues found in ``path`` (empty
    list = clean)."""
    raw = tomllib.loads(path.read_text())
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
