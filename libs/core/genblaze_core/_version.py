"""``genblaze-core`` package version — runtime view of the installed wheel.

The version is authored once in ``libs/core/pyproject.toml`` (``[project]
version = "X.Y.Z"``). Hatchling propagates it into the wheel's
``METADATA``; ``importlib.metadata.version("genblaze-core")`` reads it
back, and ``__version__`` mirrors that. So ``genblaze_core.__version__``,
``pip show genblaze-core``, and the ``b2ai-genblaze/{version}``
user-agent header always agree with each other and with whatever wheel
is actually installed — closing the version-drift footgun (storage
tranche bug #9) where a hardcoded constant here used to drift out of
sync with the published wheel.

The ``"0.0.0+unknown"`` fallback only fires when the package isn't
installed at all (e.g. ``PYTHONPATH``-style imports from an unbuilt
source tree). Editable installs (``pip install -e .``) do populate
metadata.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("genblaze-core")
except PackageNotFoundError:  # pragma: no cover — only hit in editable dev installs
    __version__ = "0.0.0+unknown"
