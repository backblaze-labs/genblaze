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
        readme_path.write_text(readme_text, encoding="utf-8")
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
    "Programming Language :: Python :: 3.11",
    "Topic :: Multimedia",
]
keywords = ["genblaze"]

[project.urls]
Homepage = "https://github.com/backblaze-labs/genblaze"
Documentation = "https://github.com/backblaze-labs/genblaze#readme"
Repository = "https://github.com/backblaze-labs/genblaze"
Issues = "https://github.com/backblaze-labs/genblaze/issues"
""",
        encoding="utf-8",
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


def test_check_package_reports_relative_link_wrapped_around_badge(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "[![Docs](https://example.com/badge.svg)](../../../docs/reference/pricing-recipes.md)\n",
    )

    issues = cpm._check_package(pyproject)

    assert issues == [
        "genblaze-example: relative markdown link in README.md:1 -> "
        "../../../docs/reference/pricing-recipes.md"
    ]


def test_check_package_ignores_mismatched_fence_marker_inside_code_block(
    tmp_path: Path,
):
    pyproject = _write_package(
        tmp_path,
        "\n".join(
            [
                "```text",
                "~~~",
                "See [pricing](../../../docs/reference/pricing-recipes.md).",
                "```",
            ]
        ),
    )

    assert cpm._check_package(pyproject) == []


def test_check_package_does_not_rescan_markdown_link_title(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "See [docs](https://example.com/docs "
        '"[ignored](../../../docs/reference/pricing-recipes.md)") and '
        '[pricing](../../../docs/reference/pricing-recipes.md "Pricing docs").\n',
    )

    assert cpm._check_package(pyproject) == [
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


def test_check_package_validates_non_markdown_readme_path(tmp_path: Path):
    pyproject = _write_package(tmp_path, None, readme_file="README.rst")

    assert cpm._check_package(pyproject) == ["genblaze-example: readme file not found: README.rst"]


def test_check_package_does_not_scan_non_markdown_readme_links(tmp_path: Path):
    pyproject = _write_package(
        tmp_path,
        "See `docs/reference/pricing-recipes.md <../../../docs/reference/pricing-recipes.md>`_.\n",
        readme_file="README.rst",
    )

    assert cpm._check_package(pyproject) == []


def test_check_package_rejects_readme_table_without_file(tmp_path: Path):
    pyproject = _write_package(tmp_path, None)
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'readme = "README.md"',
            'readme = {text = "embedded", content-type = "text/markdown"}',
        ),
        encoding="utf-8",
    )

    assert cpm._check_package(pyproject) == ["genblaze-example: readme must reference a file path"]


def test_check_package_rejects_non_string_readme_value(tmp_path: Path):
    pyproject = _write_package(tmp_path, None)
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('readme = "README.md"', "readme = 42"),
        encoding="utf-8",
    )

    assert cpm._check_package(pyproject) == ["genblaze-example: readme must reference a file path"]


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
        "See [pricing](../../../docs/reference/pricing-recipes.md).\n",
        encoding="utf-8",
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
    target.write_text(
        "See [pricing](../../../docs/reference/pricing-recipes.md).\n",
        encoding="utf-8",
    )
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


def test_check_package_rejects_redundant_license_classifier(tmp_path: Path):
    """PEP 639: a `License ::` classifier alongside `license = "MIT"` is
    redundant and setuptools >= 77 errors on the combination (#60)."""
    pyproject = _write_package(tmp_path, "See [docs](https://example.com/docs).\n")
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            '"Development Status :: 3 - Alpha",',
            '"Development Status :: 3 - Alpha",\n    "License :: OSI Approved :: MIT License",',
        ),
        encoding="utf-8",
    )

    assert cpm._check_package(pyproject) == [
        "genblaze-example: redundant `License ::` classifier alongside "
        "`license` SPDX expression (PEP 639)"
    ]


def test_check_package_allows_no_license_classifier(tmp_path: Path):
    """Baseline: `license = "MIT"` with no classifier is the desired state."""
    pyproject = _write_package(tmp_path, "See [docs](https://example.com/docs).\n")

    assert cpm._check_package(pyproject) == []


def test_check_package_rejects_readme_swapped_after_validation(
    tmp_path: Path,
    monkeypatch,
):
    pyproject = _write_package(tmp_path, "See [docs](https://example.com/docs).\n")
    original_validate = cpm._validated_readme_path

    def replacing_validate(path: Path, readme_file: str):
        readme_path, stat_result, issues = original_validate(path, readme_file)
        if readme_path is not None and stat_result is not None:
            readme_path.write_text(
                "See [pricing](../../../docs/reference/pricing-recipes.md).\n",
                encoding="utf-8",
            )
        return readme_path, stat_result, issues

    monkeypatch.setattr(cpm, "_validated_readme_path", replacing_validate)

    issues = cpm._check_package(pyproject)

    assert len(issues) == 1
    assert issues[0] == (
        "genblaze-example: readme file cannot be read: README.md: "
        "readme file changed after validation"
    )
