"""``genblaze-decart`` package version — single source of truth via importlib.metadata.

Plan 5 Phase 1B — closes the version-drift class of bug across every
genblaze connector. Reading from ``importlib.metadata`` makes the
constant always equal to whatever wheel is installed; no manual edits
per release.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("genblaze-decart")
except PackageNotFoundError:  # pragma: no cover — editable dev installs
    __version__ = "0.0.0+unknown"
