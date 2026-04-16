"""Decart Lucy video/image provider adapters for genblaze."""

from genblaze_decart.image import DecartImageProvider
from genblaze_decart.provider import DecartVideoProvider

# Backward compat: old name maps to the video provider
DecartProvider = DecartVideoProvider

__all__ = ["DecartVideoProvider", "DecartImageProvider", "DecartProvider"]
