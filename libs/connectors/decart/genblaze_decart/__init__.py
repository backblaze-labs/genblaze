"""Decart Lucy video/image provider adapters for genblaze."""

from genblaze_decart.image import DecartImageProvider
from genblaze_decart.provider import DecartVideoProvider

from ._version import __version__  # noqa: F401 — re-exported

# Backward compat: old name maps to the video provider
DecartProvider = DecartVideoProvider

__all__ = ["DecartVideoProvider", "DecartImageProvider", "DecartProvider"]
