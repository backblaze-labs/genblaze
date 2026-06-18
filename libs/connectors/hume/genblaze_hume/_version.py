"""``genblaze-hume`` package version — single source of truth via importlib.metadata.

Reading from ``importlib.metadata`` keeps the constant equal to whatever
wheel is installed; no manual edits per release.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("genblaze-hume")
except PackageNotFoundError:  # pragma: no cover — editable dev installs
    __version__ = "0.0.0+unknown"
