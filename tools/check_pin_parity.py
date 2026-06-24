#!/usr/bin/env python3
"""
Pre-publish drift guard: for every Genblaze package, compare its source
``[project.dependencies]`` to the wheel already published on PyPI at the
same version. If they diverge, fail loudly.

Why this exists
---------------
``skip-existing: true`` on ``pypa/gh-action-pypi-publish`` (set on every
publish job in ``.github/workflows/release.yml``) makes the release
workflow idempotent — re-running a partially-failed release silently
no-ops packages that already published. That's the desired behavior
when the source version matches what's on PyPI *and the contents
match*.

The trap: if a maintainer widens a dependency constraint in source
(e.g. ``genblaze-core>=0.2.0,<0.3`` → ``<0.4``) but forgets to bump the
package's own ``version``, ``skip-existing`` will skip publishing on
every subsequent wave. The corrected wheel never reaches PyPI; the
broken wheel stays the resolvable one. This bug has shipped twice:

* 0.3.0 wave: ``genblaze-s3`` shipped with stale ``genblaze-core<0.3``;
  fixed in 0.3.1 by bumping ``genblaze-s3`` so a fresh wheel could land.
* 0.3.2 wave: ``genblaze-langsmith`` shipped with stale
  ``genblaze-core<0.3``; fixed in 0.3.3.

This script runs before any publish job and fails the workflow if any
package would be silently skipped despite divergent metadata. The fix
is always the same: bump the package's source ``version`` and re-run.

How comparison works
--------------------
For each package:

1. Read source ``[project.dependencies]`` from its ``pyproject.toml``.
2. Fetch ``https://pypi.org/pypi/<name>/<version>/json`` and extract
   the wheel's base ``Requires-Dist`` (entries without ``; extra ==``).
3. Parse both sides with ``packaging.requirements.Requirement``,
   normalize specifier sets to sorted form, compare.

Skipped cases (not errors):

* Package source version is not on PyPI yet — a fresh wheel will
  publish; nothing to compare.
* Package's source ``[project.dependencies]`` is empty AND PyPI's
  ``Requires-Dist`` for base deps is empty.

Exit codes: 0 on parity, 1 on drift, 2 on unexpected error.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# tomllib is stdlib on 3.11+. We officially require 3.11 (see each
# pyproject.toml), but fall back to ``tomli`` on 3.10 so this script
# stays runnable in minimal CI containers and contributor envs.
try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        print(
            "ERROR: this script needs tomllib (Python 3.11+) or `pip install tomli`.",
            file=sys.stderr,
        )
        sys.exit(2)

try:
    from packaging.requirements import Requirement
except ImportError:
    print(
        "ERROR: this script requires the `packaging` library.\n"
        "Install with: pip install packaging\n"
        "(packaging is a transitive dep of pip itself; if you're seeing this, "
        "you're in an unusually minimal venv.)",
        file=sys.stderr,
    )
    sys.exit(2)


# Every package the release workflow publishes. Order doesn't matter
# for this check, but it mirrors the workflow's publish graph so
# diffs to the package list stay co-located.
PACKAGES: list[str] = [
    "libs/core",
    "libs/connectors/replicate",
    "libs/connectors/s3",
    "libs/connectors/openai",
    "libs/connectors/google",
    "libs/connectors/runway",
    "libs/connectors/luma",
    "libs/connectors/decart",
    "libs/connectors/elevenlabs",
    "libs/connectors/stability-audio",
    "libs/connectors/lmnt",
    "libs/connectors/hume",
    "libs/connectors/gmicloud",
    "libs/connectors/langsmith",
    "libs/connectors/nvidia",
    "libs/connectors/assemblyai",
    "cli",
    "libs/meta",
]


def normalize(req_str: str) -> tuple:
    """Reduce a PEP 508 requirement string to a comparable tuple.

    PyPI's ``Requires-Dist`` and the source ``[project.dependencies]``
    can render the same constraint with different specifier ordering
    (``<0.4,>=0.3.0`` vs ``>=0.3.0,<0.4``) or whitespace. Parse with
    ``packaging`` and reduce to (name, sorted-specifiers, extras,
    marker) so equivalence comparison is structural.
    """
    r = Requirement(req_str)
    return (
        r.name.lower().replace("_", "-"),
        tuple(sorted(str(s) for s in r.specifier)),
        tuple(sorted(r.extras)),
        str(r.marker) if r.marker else None,
    )


def base_deps_from_pypi(requires_dist: list[str] | None) -> list[str]:
    """Filter PyPI ``Requires-Dist`` to entries without an extra marker.

    PyPI returns every dependency — base AND extras — flattened into a
    single list. Extras are marked with ``; extra == "name"``. We only
    compare base deps because that's where the trap lives; extras are
    handled by ``check_pypi_metadata.py``.
    """
    if not requires_dist:
        return []
    return [d for d in requires_dist if "extra ==" not in d]


def fetch_pypi_metadata(name: str, version: str) -> dict | None:
    """Return the PyPI JSON metadata for ``<name>==<version>``, or None on 404.

    A 404 means the source version hasn't been published yet — a fresh
    wheel will publish on this release, so there's nothing to compare.
    Any other HTTP error is fatal: we don't want to silently skip the
    parity check because PyPI was briefly unreachable.
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def check_package(pkg_path: Path, repo_root: Path) -> tuple[str, str, list[str] | None]:
    """Check one package. Returns (status, name, details).

    Status is one of:
      "match"      — source and PyPI agree
      "unreleased" — source version not on PyPI yet (fresh publish)
      "drift"      — divergence detected; details is a list of lines for the error report
    """
    pyproject = repo_root / pkg_path / "pyproject.toml"
    with open(pyproject, "rb") as f:
        cfg = tomllib.load(f)
    name = cfg["project"]["name"]
    version = cfg["project"]["version"]
    source_deps: list[str] = cfg["project"].get("dependencies", [])

    metadata = fetch_pypi_metadata(name, version)
    if metadata is None:
        return ("unreleased", f"{name}=={version}", None)

    pypi_deps = base_deps_from_pypi(metadata.get("info", {}).get("requires_dist"))

    src_norm = sorted(normalize(d) for d in source_deps)
    pypi_norm = sorted(normalize(d) for d in pypi_deps)

    if src_norm == pypi_norm:
        return ("match", f"{name}=={version}", None)

    # Build a maintainer-friendly diff report.
    src_set = {normalize(d): d for d in source_deps}
    pypi_set = {normalize(d): d for d in pypi_deps}
    only_in_source = sorted(src_set[k] for k in src_set if k not in pypi_set)
    only_on_pypi = sorted(pypi_set[k] for k in pypi_set if k not in src_set)

    lines: list[str] = []
    lines.append(f"{name}=={version}")
    if only_in_source:
        lines.append("    Only in source (would publish):")
        for d in only_in_source:
            lines.append(f"      + {d}")
    if only_on_pypi:
        lines.append(f"    Only on PyPI {version} wheel (would be silently kept):")
        for d in only_on_pypi:
            lines.append(f"      - {d}")
    return ("drift", f"{name}=={version}", lines)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    drift_reports: list[list[str]] = []
    unreleased: list[str] = []
    matched: list[str] = []

    for pkg_path in PACKAGES:
        try:
            status, label, details = check_package(Path(pkg_path), repo_root)
        except Exception as e:
            print(f"ERROR: failed to check {pkg_path}: {e}", file=sys.stderr)
            return 2
        if status == "match":
            matched.append(label)
            print(f"  ok    {label}: source pins match PyPI wheel")
        elif status == "unreleased":
            unreleased.append(label)
            print(f"  fresh {label}: not yet on PyPI — will publish")
        elif status == "drift":
            assert details is not None
            drift_reports.append(details)
            print(f"  DRIFT {label}: source pins diverge from PyPI wheel")

    print()
    print(f"Summary: {len(matched)} parity, {len(unreleased)} fresh, {len(drift_reports)} drift")

    if drift_reports:
        print()
        print(
            "ERROR: one or more packages have source dependencies that differ\n"
            "from the wheel already published on PyPI at the same version.\n"
            "skip-existing would silently no-op these wheels, leaving the\n"
            "stale ones resolvable forever.\n"
        )
        for report in drift_reports:
            for line in report:
                print(line)
            print()
        print(
            "Fix: bump the affected package's version in its pyproject.toml\n"
            "so a corrected wheel can publish. See RELEASING.md.\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
