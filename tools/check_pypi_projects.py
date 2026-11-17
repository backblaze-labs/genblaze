#!/usr/bin/env python3
"""
Pre-release warning: flag publishable Python packages with no pypi.org project yet.

A package's first publish only succeeds if a *pending* Trusted Publisher was
registered on pypi.org beforehand. Without one, ``release.yml`` fails with
``403 Invalid API Token: OIDC scoped token is not valid for project`` (the
0.8.0 wave hit this for genblaze-fal and genblaze-atlascloud).

The check is warning-only (always exits 0 once it has run): PyPI's public JSON
API cannot see pending publishers, so a correctly prepared new package still
looks missing until its first upload. Blocking on it would block every
legitimate new connector.

Statuses per package (``GET https://pypi.org/pypi/<name>/json``):

* ``exists``  -- 200: the project is on PyPI, so its publisher already worked.
* ``new``     -- 404: no project yet; the maintainer must confirm a pending
  Trusted Publisher exists.
* ``unknown`` -- any other HTTP error, timeout or network failure. Never
  reported as ``new``: an outage must not send anyone to add publishers.

The package set comes from ``prepare_release.discover_packages`` (Python
packages only; npm has its own Trusted Publisher, RELEASING.md §c).
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

from prepare_release import discover_packages

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
TIMEOUT_SECONDS = 10

EXISTS = "exists"
NEW = "new"
UNKNOWN = "unknown"

# Must match the publish job in .github/workflows/release.yml.
_PUBLISHER_FIELDS = (
    ("Owner", "backblaze-labs"),
    ("Repository", "genblaze"),
    ("Workflow", "release.yml"),
    ("Environment", "pypi"),
)


def project_status(name: str) -> str:
    """Return EXISTS, NEW or UNKNOWN for one PyPI project name."""
    url = PYPI_JSON_URL.format(name=name)
    try:
        # Fixed https:// URL; the name comes from the repo's own pyproject files.
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS):  # noqa: S310
            return EXISTS
    except urllib.error.HTTPError as e:
        # HTTPError subclasses URLError/OSError, so it must be caught first.
        return NEW if e.code == 404 else UNKNOWN
    except OSError:  # URLError, timeouts, connection resets
        return UNKNOWN


def publishable_names(repo_root: Path) -> list[str]:
    """Sorted distribution names of every Python package the release publishes."""
    packages = discover_packages(repo_root).values()
    return sorted(p.name for p in packages if p.kind == "pyproject")


def _render_banner(new: list[str]) -> list[str]:
    rule = "=" * 72
    lines = [
        rule,
        "WARNING: these packages have no project on pypi.org yet:",
        *(f"  - {name}" for name in new),
        "",
        "Their first publish fails (403 'OIDC scoped token is not valid for",
        "project') unless a pending Trusted Publisher exists. Before tagging,",
        "add one at https://pypi.org/manage/account/publishing/ for each:",
        *(f"  {label + ':':<13}{value}" for label, value in _PUBLISHER_FIELDS),
        "",
        "PyPI cannot report pending publishers, so this warning persists until",
        "the first upload. If they are already added, carry on.",
        "See RELEASING.md §a (One-time setup).",
        rule,
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    del argv  # no options; signature matches the other tools/ entry points
    repo_root = Path(__file__).resolve().parent.parent
    results = {name: project_status(name) for name in publishable_names(repo_root)}

    marks = {EXISTS: "ok   ", NEW: "NEW  ", UNKNOWN: "??   "}
    for name, status in results.items():
        print(f"  {marks[status]} {name}")

    new = [n for n, s in results.items() if s == NEW]
    unknown = [n for n, s in results.items() if s == UNKNOWN]
    print()
    print(
        f"Summary: {len(results) - len(new) - len(unknown)} on PyPI, "
        f"{len(new)} new, {len(unknown)} unverified"
    )

    if unknown:
        print()
        print("Could not reach PyPI to verify (re-run when online):")
        print("\n".join(f"  - {name}" for name in unknown))

    # Banner last so it sits directly above make's final success message.
    if new:
        print()
        print("\n".join(_render_banner(new)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
