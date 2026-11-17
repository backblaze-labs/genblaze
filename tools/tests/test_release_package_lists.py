"""Package-list parity: every hand-maintained list of publishable Python packages
must match ``prepare_release.discover_packages``.

Two lists are hardcoded today:

* ``.github/workflows/release.yml``: the publish jobs (core, cli, meta, and
  the ``publish-connectors`` matrix). A package missing here is never
  published.
* ``tools/check_pin_parity.PACKAGES``: a package missing here skips the
  drift guard.

Lists are compared by repo-relative directory (``libs/core``,
``libs/connectors/fal``, ...), the key both lists use.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import yaml

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_pin_parity  # noqa: E402
import prepare_release  # noqa: E402

_REPO_ROOT = _TOOLS_DIR.parent
_RELEASE_YML = _REPO_ROOT / ".github/workflows/release.yml"
_PYPI_PUBLISH_ACTION = "pypa/gh-action-pypi-publish@"
_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.(\w+)\s*\}\}")


def _discovered_dirs() -> set[str]:
    packages = prepare_release.discover_packages(_REPO_ROOT).values()
    return {p.rel_dir for p in packages if p.kind == "pyproject"}


def _load_release_workflow() -> dict:
    return yaml.safe_load(_RELEASE_YML.read_text(encoding="utf-8"))


def published_dirs(workflow: dict) -> set[str]:
    """Package dirs uploaded by every PyPI publish step in ``workflow``.

    Reads each ``pypa/gh-action-pypi-publish`` step's ``packages-dir`` (e.g.
    ``libs/core/dist/``) and expands a ``${{ matrix.<key> }}`` reference
    against that job's matrix, so this follows what actually uploads rather
    than job names.
    """
    dirs: set[str] = set()
    for job in workflow["jobs"].values():
        matrix = job.get("strategy", {}).get("matrix", {})
        for step in job.get("steps", []):
            if not step.get("uses", "").startswith(_PYPI_PUBLISH_ACTION):
                continue
            pkg_dir = step["with"]["packages-dir"].rstrip("/").removesuffix("/dist")
            ref = _MATRIX_REF.search(pkg_dir)
            if ref is None:
                dirs.add(pkg_dir)
                continue
            for value in matrix[ref.group(1)]:
                dirs.add(_MATRIX_REF.sub(str(value), pkg_dir))
    return dirs


def mismatch_report(list_name: str, listed: set[str], fix_hint: str) -> str | None:
    """Describe how ``listed`` differs from discovered packages, or None if equal."""
    expected = _discovered_dirs()
    missing = sorted(expected - listed)
    extra = sorted(listed - expected)
    if not missing and not extra:
        return None
    lines = [f"{list_name} is out of sync with the packages on disk."]
    if missing:
        lines.append(f"  Missing from {list_name}: {', '.join(missing)}")
        lines.append(f"  Fix: {fix_hint}")
    if extra:
        lines.append(f"  Listed in {list_name} but no pyproject.toml found: {', '.join(extra)}")
    return "\n".join(lines)


_RELEASE_HINT = (
    "add each libs/connectors/<name> as `- <name>` under "
    "jobs.publish-connectors.strategy.matrix.connector in .github/workflows/release.yml "
    "(core, cli and meta have their own publish-* jobs)."
)
_PIN_PARITY_HINT = "add the directory to PACKAGES in tools/check_pin_parity.py."


# ---------------------------------------------------------------------------
# Parity against the real lists
# ---------------------------------------------------------------------------


def test_release_workflow_publishes_every_discovered_package():
    report = mismatch_report(
        "release.yml publish jobs", published_dirs(_load_release_workflow()), _RELEASE_HINT
    )
    assert report is None, report


def test_pin_parity_checks_every_discovered_package():
    report = mismatch_report(
        "check_pin_parity.PACKAGES", set(check_pin_parity.PACKAGES), _PIN_PARITY_HINT
    )
    assert report is None, report


# ---------------------------------------------------------------------------
# The parity check catches drift (lists mutated in memory, files untouched)
# ---------------------------------------------------------------------------


def test_published_dirs_covers_fixed_jobs_and_matrix():
    dirs = published_dirs(_load_release_workflow())
    assert {"libs/core", "cli", "libs/meta", "libs/connectors/fal"} <= dirs


def test_connector_dropped_from_release_matrix_is_named():
    workflow = copy.deepcopy(_load_release_workflow())
    workflow["jobs"]["publish-connectors"]["strategy"]["matrix"]["connector"].remove("fal")
    report = mismatch_report("release.yml publish jobs", published_dirs(workflow), _RELEASE_HINT)
    assert report is not None
    assert "Missing from release.yml publish jobs: libs/connectors/fal" in report
    assert "publish-connectors" in report


def test_dropped_publish_job_is_named():
    workflow = copy.deepcopy(_load_release_workflow())
    del workflow["jobs"]["publish-cli"]
    report = mismatch_report("release.yml publish jobs", published_dirs(workflow), _RELEASE_HINT)
    assert report is not None and "Missing from release.yml publish jobs: cli" in report


def test_package_dropped_from_pin_parity_is_named():
    listed = set(check_pin_parity.PACKAGES) - {"libs/connectors/atlascloud"}
    report = mismatch_report("check_pin_parity.PACKAGES", listed, _PIN_PARITY_HINT)
    assert report is not None
    assert "Missing from check_pin_parity.PACKAGES: libs/connectors/atlascloud" in report
    assert "tools/check_pin_parity.py" in report


def test_stale_entry_without_pyproject_is_named():
    listed = set(check_pin_parity.PACKAGES) | {"libs/connectors/removed"}
    report = mismatch_report("check_pin_parity.PACKAGES", listed, _PIN_PARITY_HINT)
    assert report is not None
    assert "no pyproject.toml found: libs/connectors/removed" in report
