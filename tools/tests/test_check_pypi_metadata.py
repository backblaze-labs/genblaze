"""Tests for tools/check_pypi_metadata.py."""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_pypi_metadata as cpm  # noqa: E402


def _write_package(
    tmp_path: Path,
    readme_text: str | None,
    *,
    readme_file: str = "README.md",
) -> Path:
    package_dir = tmp_path / "libs" / "example"
    package_dir.mkdir(parents=True)
    if readme_text is not None:
        readme_path = package_dir / readme_file
        readme_path.parent.mkdir(parents=True, exist_ok=True)
        readme_path.write_text(readme_text)
    pyproject = package_dir / "pyproject.toml"
    pyproject.write_text(
        f"""[project]
name = "genblaze-example"
version = "0.1.0"
description = "Example package for metadata tests"
authors = [{{name = "Backblaze Labs"}}]
readme = "{readme_file}"
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


def test_check_package_reports_relative_link_with_nested_bracket_label(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "See [pricing [recipes]](../../../docs/reference/pricing-recipes.md).\n",
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


def test_check_package_rejects_unsupported_readme_link_schemes(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "\n".join(
            [
                "Avoid [script](javascript:alert(1)).",
                "Avoid [local](file:///tmp/secret.md).",
                r"Avoid [drive](C:\docs\readme.md).",
            ]
        ),
    )

    assert cpm._check_package(pyproject) == [
        "genblaze-example: unsupported markdown link scheme in README.md:1 -> javascript:alert(1)",
        "genblaze-example: unsupported markdown link scheme in README.md:2 -> "
        + "file:///tmp/secret.md",
        "genblaze-example: unsupported markdown link scheme in README.md:3 -> "
        + r"C:\docs\readme.md",
    ]


def test_check_package_rejects_absolute_readme_path(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        None,
        readme_file=str(tmp_path / "README.md"),
    )

    assert cpm._check_package(pyproject) == [
        f"genblaze-example: readme path must be relative: {tmp_path / 'README.md'}"
    ]


def test_check_package_rejects_out_of_package_readme_path(tmp_path: Path):
    (tmp_path / "outside.md").write_text(
        "See [pricing](../../../docs/reference/pricing-recipes.md).\n"
    )
    pyproject = _write_package(
        tmp_path,
        None,
        readme_file="../../outside.md",
    )

    assert cpm._check_package(pyproject) == [
        "genblaze-example: readme path must stay within package: ../../outside.md"
    ]


def test_check_package_rejects_symlinked_readme_without_reading_target(tmp_path: Path):
    target = tmp_path / "target.md"
    target.write_text("See [pricing](../../../docs/reference/pricing-recipes.md).\n")
    pyproject = _write_package(tmp_path, None)
    (pyproject.parent / "README.md").symlink_to(target)

    assert cpm._check_package(pyproject) == [
        "genblaze-example: readme path must not contain symlinks: README.md"
    ]


def test_check_package_rejects_non_regular_readme(tmp_path: Path):
    pyproject = _write_package(tmp_path, None)
    (pyproject.parent / "README.md").mkdir()

    assert cpm._check_package(pyproject) == [
        "genblaze-example: readme path is not a regular file: README.md"
    ]


def test_check_package_rejects_oversized_readme_before_reading(tmp_path: Path):
    pyproject = _write_package(tmp_path, "x" * (cpm._README_MAX_BYTES + 1))

    assert cpm._check_package(pyproject) == [
        "genblaze-example: readme file too large: README.md "
        f"({cpm._README_MAX_BYTES + 1} bytes > {cpm._README_MAX_BYTES})"
    ]


def test_check_package_reports_unreadable_readme(tmp_path: Path):
    pyproject = _write_package(tmp_path, None)
    (pyproject.parent / "README.md").write_bytes(b"\xff")

    issues = cpm._check_package(pyproject)

    assert len(issues) == 1
    assert issues[0].startswith("genblaze-example: readme file cannot be read: README.md: ")
