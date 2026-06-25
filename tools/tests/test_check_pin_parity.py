"""Tests for tools/check_pin_parity.py.

Covers the core parsing and comparison logic without hitting the network.
All PyPI-fetching is replaced by fixtures that return pre-canned metadata.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# The tools/ dir is not on sys.path by default. Add its parent so we
# can import check_pin_parity as a module.
_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_pin_parity as cpp  # noqa: E402


# ---------------------------------------------------------------------------
# normalize() — parsing and structural equivalence
# ---------------------------------------------------------------------------


def test_normalize_simple():
    assert cpp.normalize("packaging>=21.0") == ("packaging", (">=21.0",), (), None)


def test_normalize_name_is_lowercased_and_canonicalized():
    assert cpp.normalize("My_Package>=1.0")[0] == "my-package"


def test_normalize_multi_specifier_is_sorted():
    a = cpp.normalize("foo<0.4,>=0.3.0")
    b = cpp.normalize("foo>=0.3.0,<0.4")
    assert a == b


# ---------------------------------------------------------------------------
# base_deps_from_pypi() — strips extras entries
# ---------------------------------------------------------------------------


def test_base_deps_strips_extra_markers():
    requires_dist = [
        "genblaze-core>=0.3.0,<0.4",
        'genblaze-openai>=0.3.0,<0.4; extra == "openai"',
        'genblaze-all>=0.3.0,<0.4; extra == "all"',
    ]
    result = cpp.base_deps_from_pypi(requires_dist)
    assert result == ["genblaze-core>=0.3.0,<0.4"]


def test_base_deps_handles_none():
    assert cpp.base_deps_from_pypi(None) == []


def test_base_deps_handles_empty():
    assert cpp.base_deps_from_pypi([]) == []


# ---------------------------------------------------------------------------
# extras_from_pypi() — NEW: groups extras by name
# ---------------------------------------------------------------------------


def test_extras_from_pypi_groups_by_extra_name():
    requires_dist = [
        "genblaze-core>=0.3.0,<0.4",
        'genblaze-openai>=0.3.0,<0.4; extra == "openai"',
        'genblaze-runway>=0.3.0,<0.4; extra == "video"',
        'genblaze-luma>=0.3.0,<0.4; extra == "video"',
    ]
    result = cpp.extras_from_pypi(requires_dist)
    assert "openai" in result
    assert "video" in result
    assert len(result["openai"]) == 1
    assert len(result["video"]) == 2
    # Base dep must not appear in any extra group
    assert not any("genblaze-core" in dep for deps in result.values() for dep in deps)


def test_extras_from_pypi_handles_none():
    assert cpp.extras_from_pypi(None) == {}


def test_extras_from_pypi_handles_no_extras():
    assert cpp.extras_from_pypi(["genblaze-core>=0.3.0"]) == {}


def test_extras_from_pypi_strips_extra_marker_from_dep_string():
    """Deps returned must be plain requirement strings, not with the marker."""
    requires_dist = ['genblaze-openai>=0.3.0,<0.4; extra == "openai"']
    result = cpp.extras_from_pypi(requires_dist)
    dep = result["openai"][0]
    assert "extra ==" not in dep
    # Must be parseable as a plain requirement
    from packaging.requirements import Requirement

    Requirement(dep)


def test_extras_from_pypi_single_quoted_marker():
    """The marker may use single quotes; the name must still parse."""
    requires_dist = ["genblaze-openai>=0.3.0,<0.4; extra == 'openai'"]
    result = cpp.extras_from_pypi(requires_dist)
    assert result["openai"] == ["genblaze-openai>=0.3.0,<0.4"]


def test_extras_from_pypi_compound_marker_extra_not_last():
    """Regression: a compound marker with ``extra`` *first* must not corrupt
    the extra name (the old string-split produced a garbage key)."""
    requires_dist = ['foo>=1.0; extra == "all" and python_version >= "3.11"']
    result = cpp.extras_from_pypi(requires_dist)
    assert list(result.keys()) == ["all"]
    # The residual (non-extra) marker is preserved on the requirement.
    assert 'python_version >= "3.11"' in result["all"][0]


def test_extras_from_pypi_canonicalizes_extra_name():
    """PEP 685: an underscore/case-variant extra name canonicalizes to dashes."""
    requires_dist = ['foo>=1.0; extra == "Stability_Audio"']
    result = cpp.extras_from_pypi(requires_dist)
    assert list(result.keys()) == ["stability-audio"]


# ---------------------------------------------------------------------------
# split_extra() / canon_extra() — marker parsing primitives
# ---------------------------------------------------------------------------


def test_split_extra_base_dep_has_no_extra():
    assert cpp.split_extra("genblaze-core>=0.3.0,<0.4") == (None, "genblaze-core>=0.3.0,<0.4")


def test_split_extra_non_extra_marker_is_not_an_extra():
    """A dep with only a python_version marker is a base dep, not an extra."""
    name, req = cpp.split_extra('foo>=1.0; python_version < "3.12"')
    assert name is None


def test_split_extra_preserves_residual_marker():
    """The non-extra portion of a compound marker survives the split."""
    name, req = cpp.split_extra('foo>=1.0; python_version < "3.12" and extra == "bar"')
    assert name == "bar"
    # Re-normalized, the residual marker must match the same source dep.
    assert cpp.normalize(req) == cpp.normalize('foo>=1.0; python_version < "3.12"')


def test_canon_extra_normalizes():
    assert cpp.canon_extra("Stability_Audio") == "stability-audio"
    assert cpp.canon_extra("video") == "video"


# ---------------------------------------------------------------------------
# check_package() — detects extras drift
# ---------------------------------------------------------------------------


def _make_pyproject(
    tmp: Path,
    *,
    name: str = "genblaze",
    version: str = "0.4.0",
    base_deps: list[str] | None = None,
    optional_deps: dict[str, list[str]] | None = None,
) -> Path:
    """Write a minimal pyproject.toml under tmp/ and return its directory."""
    base_deps = base_deps or []
    optional_deps = optional_deps or {}

    base_block = "\n".join(f'    "{d}",' for d in base_deps)
    extras_lines: list[str] = []
    for extra_name, deps in optional_deps.items():
        deps_str = "\n".join(f'    "{d}",' for d in deps)
        extras_lines.append(f"[project.optional-dependencies]\n{extra_name} = [\n{deps_str}\n]")

    content = f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "{version}"
description = "test"
requires-python = ">=3.11"
dependencies = [
{base_block}
]

{"".join(extras_lines)}
"""
    pkg_dir = tmp / "libs" / "meta"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "pyproject.toml").write_text(content)
    return pkg_dir


def _pypi_metadata(
    *,
    base: list[str] | None = None,
    extras: dict[str, list[str]] | None = None,
) -> dict:
    """Build a minimal PyPI JSON metadata dict."""
    requires_dist: list[str] = list(base or [])
    for extra_name, deps in (extras or {}).items():
        for dep in deps:
            requires_dist.append(f'{dep}; extra == "{extra_name}"')
    return {"info": {"requires_dist": requires_dist}}


def test_check_package_reports_match_when_extras_agree():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.4"],
            optional_deps={"openai": ["genblaze-openai>=0.3.0,<0.4"]},
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4"],
            extras={"openai": ["genblaze-openai>=0.3.0,<0.4"]},
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "match"
    assert details is None


def test_check_package_detects_extra_added_in_source():
    """Source has a new connector in [all]; PyPI wheel doesn't — drift."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.4"],
            optional_deps={
                "all": [
                    "genblaze-openai>=0.3.0,<0.4",
                    "genblaze-newpkg>=0.1.0,<0.4",  # added in source, not on PyPI
                ]
            },
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4"],
            extras={"all": ["genblaze-openai>=0.3.0,<0.4"]},
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "drift"
    assert details is not None
    combined = "\n".join(details)
    # Must name the extra in the report
    assert "[all]" in combined
    assert "genblaze-newpkg" in combined


def test_check_package_detects_extra_removed_from_source():
    """PyPI has a dep in [video]; source dropped it — drift."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.4"],
            optional_deps={
                "video": ["genblaze-runway>=0.3.0,<0.4"],  # decart was removed from source
            },
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4"],
            extras={
                "video": [
                    "genblaze-runway>=0.3.0,<0.4",
                    "genblaze-decart>=0.3.0,<0.4",  # still on PyPI
                ]
            },
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "drift"
    assert details is not None
    combined = "\n".join(details)
    assert "[video]" in combined
    assert "genblaze-decart" in combined


def test_check_package_detects_pin_widened_in_extra():
    """Same connector in [all] but constraint widened without version bump — drift."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.5"],  # widened base too
            optional_deps={"all": ["genblaze-openai>=0.3.0,<0.5"]},  # widened
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4"],
            extras={"all": ["genblaze-openai>=0.3.0,<0.4"]},
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "drift"


def test_check_package_base_only_still_works():
    """Regression: non-umbrella packages (no optional-deps) keep working."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            name="genblaze-openai",
            base_deps=["genblaze-core>=0.3.0,<0.4", "openai>=1.0"],
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4", "openai>=1.0"],
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "match"


def test_check_package_unreleased_skips_extras_check():
    """Fresh packages (not on PyPI) should still return 'unreleased'."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            optional_deps={"all": ["genblaze-openai>=0.3.0,<0.4"]},
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=None):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "unreleased"


def test_check_package_extra_only_on_pypi_reports_drift():
    """Source has no optional-deps; PyPI wheel has [all] — drift."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.4"],
            # No optional-deps in source
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4"],
            extras={"all": ["genblaze-openai>=0.3.0,<0.4"]},
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "drift"
    assert details is not None
    combined = "\n".join(details)
    assert "[all]" in combined


def test_check_package_extra_name_normalization_matches():
    """Source key uses underscores; PyPI uses dashes — same logical extra,
    so no drift once both are canonicalized (PEP 685)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.4"],
            optional_deps={"stability_audio": ["genblaze-stability-audio>=0.3.0,<0.4"]},
        )
        metadata = _pypi_metadata(
            base=["genblaze-core>=0.3.0,<0.4"],
            extras={"stability-audio": ["genblaze-stability-audio>=0.3.0,<0.4"]},
        )
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "match"


def test_check_package_conditional_dep_in_extra_matches():
    """A python_version-conditional dep inside an extra must compare equal:
    PyPI appends ``and extra == "x"`` to the source marker, and the gate must
    strip only the extra clause, not the python_version one."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_dir = _make_pyproject(
            tmp,
            base_deps=["genblaze-core>=0.3.0,<0.4"],
            # Single-quoted marker keeps the double-quoted TOML string valid;
            # PEP 508 accepts either quote style.
            optional_deps={"all": ["tomli>=2.0; python_version < '3.11'"]},
        )
        # PyPI renders the flattened marker with the extra clause appended.
        metadata = {
            "info": {
                "requires_dist": [
                    "genblaze-core>=0.3.0,<0.4",
                    'tomli>=2.0; python_version < "3.11" and extra == "all"',
                ]
            }
        }
        with patch.object(cpp, "fetch_pypi_metadata", return_value=metadata):
            status, label, details = cpp.check_package(pkg_dir.relative_to(tmp), tmp)
    assert status == "match", details


# ---------------------------------------------------------------------------
# main() — exit-code contract (what CI actually depends on)
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_parity():
    with (
        patch.object(cpp, "PACKAGES", ["libs/meta"]),
        patch.object(cpp, "check_package", return_value=("match", "genblaze==0.4.0", None)),
    ):
        assert cpp.main() == 0


def test_main_returns_one_on_drift():
    drift = ("drift", "genblaze==0.4.0", ["genblaze==0.4.0", "  [all]"])
    with (
        patch.object(cpp, "PACKAGES", ["libs/meta"]),
        patch.object(cpp, "check_package", return_value=drift),
    ):
        assert cpp.main() == 1


def test_main_returns_two_on_unexpected_error():
    with (
        patch.object(cpp, "PACKAGES", ["libs/meta"]),
        patch.object(cpp, "check_package", side_effect=RuntimeError("boom")),
    ):
        assert cpp.main() == 2
