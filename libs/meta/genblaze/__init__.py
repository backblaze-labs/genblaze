"""genblaze — umbrella import surface for the genblaze SDK.

Compatibility shim so ``pip install genblaze`` + ``import genblaze`` works the
way most Python users expect. Every top-level symbol is re-exported verbatim
from :mod:`genblaze_core`, which remains the canonical module name used
throughout the documentation and examples::

    from genblaze import Pipeline, Modality, ObjectStorageSink
    # equivalent to
    from genblaze_core import Pipeline, Modality, ObjectStorageSink

Lookups are lazy — importing this module does not eagerly load every submodule
of ``genblaze_core``; individual symbols pay their import cost on first access.

Not mirrored by the shim:

* Nested submodules (``genblaze_core.media``, ``genblaze_core.canonical``).
  Import those via ``genblaze_core`` directly.
* Provider adapters (``SoraProvider``, ``VeoProvider`` and friends). Each
  adapter ships as its own package — install via an extra
  (``pip install "genblaze[openai]"``) and import from the package
  (``from genblaze_openai import SoraProvider``).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "0.3.0"

_core = importlib.import_module("genblaze_core")
__all__ = list(_core.__all__)


def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(f"module 'genblaze' has no attribute {name!r}")
    try:
        val = getattr(_core, name)
    except AttributeError:
        raise AttributeError(
            f"module 'genblaze' has no attribute {name!r}. "
            f"For nested submodules (e.g. genblaze_core.media) import from "
            f"genblaze_core directly. For provider adapters, install the "
            f"connector and import from its own package, e.g. "
            f"`from genblaze_openai import SoraProvider` after "
            f'`pip install "genblaze[openai]"`.'
        ) from None
    globals()[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals().keys()))


if TYPE_CHECKING:
    # Static type-checkers see the full core surface; runtime uses __getattr__.
    from genblaze_core import *  # noqa: F401, F403
