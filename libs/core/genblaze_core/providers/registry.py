"""Provider discovery via entry points."""

from __future__ import annotations

import importlib.metadata
import logging

from genblaze_core.providers.base import BaseProvider

logger = logging.getLogger("genblaze.provider")

ENTRY_POINT_GROUP = "genblaze.providers"


def discover_providers() -> dict[str, type[BaseProvider]]:
    """Discover installed provider plugins via entry points.

    Returns a dict mapping provider name → provider class for all packages
    that register under the ``genblaze.providers`` entry point group.
    """
    providers: dict[str, type[BaseProvider]] = {}
    # group= kwarg available since Python 3.9; project requires 3.11+
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, BaseProvider):
                providers[ep.name] = cls
            else:
                logger.warning("Entry point %r is not a BaseProvider subclass", ep.name)
        except Exception:
            logger.warning("Failed to load provider entry point %r", ep.name, exc_info=True)
    return providers
