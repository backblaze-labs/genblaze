"""Tests for tools/check_pypi_metadata.py."""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_pypi_metadata as cpm  # noqa: E402


def _write_package(tmp_path: Path, readme_text: str) -> Path:
    package_dir = tmp_path / "libs" / "example"
    package_dir.mkdir(parents=True)
    (package_dir / "README.md").write_text(readme_text)
    pyproject = package_dir / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "genblaze-example"
version = "0.1.0"
description = "Example package for metadata tests"
authors = [{name = "Backblaze Labs"}]
readme = "README.md"
requires-python = ">=3.11"
license = "MIT"
classifiers = [
    "Development Status :: 3 - Alpha",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Topic :: Multimedia",
]
keywords = ["genblaze"]

[project.urls]
Homepage = "https://github.com/backblaze-labs/genblaze"
Documentation = "https://github.com/backblaze-labs/genblaze#readme"
Repository = "https://github.com/backblaze-labs/genblaze"
Issues = "https://github.com/backblaze-labs/genblaze/issues"
"""
    )
    return pyproject


def test_check_package_reports_relative_readme_links(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "See [pricing](../../../docs/reference/pricing-recipes.md).\n",
    )

    issues = cpm._check_package(pyproject)

    assert issues == [
        "genblaze-example: relative markdown link in README.md:1 -> "
        "../../../docs/reference/pricing-recipes.md"
    ]


def test_check_package_allows_absolute_readme_links_and_anchors(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "\n".join(
            [
                "See [pricing](https://github.com/backblaze-labs/genblaze/blob/main/docs/reference/pricing-recipes.md).",
                "Jump to [usage](#usage).",
                "Contact [maintainers](mailto:oss@backblaze.com).",
            ]
        ),
    )

    assert cpm._check_package(pyproject) == []
