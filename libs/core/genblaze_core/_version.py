"""``genblaze-core`` package version — single source of truth.

Plan 5 Phase 1A — closes the "version drift" footgun (bug #9 in the
storage tranche): pre-fix, ``genblaze_core.__version__`` was a
hardcoded constant that drifted out of sync with the wheel's
``importlib.metadata.version("genblaze-core")`` and the
``b2ai-genblaze/{version}`` user-agent header. After every release
bump, three places needed editing; mistakes silently shipped wrong
versions to logs and B2 attribution.

Now ``__version__`` reads from ``importlib.metadata`` so it's
always whatever the installed wheel reports. The fallback string
``"0.0.0+unknown"`` covers editable installs without metadata
(e.g. mid-development ``pip install -e .`` from a fresh clone
before the package is built).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("genblaze-core")
except PackageNotFoundError:  # pragma: no cover — only hit in editable dev installs
    __version__ = "0.0.0+unknown"
