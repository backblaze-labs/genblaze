"""Stability AI Stable Audio provider adapter for genblaze."""

from genblaze_stability_audio.provider import StabilityAudioProvider

from ._version import __version__  # noqa: F401 — re-exported

__all__ = ["StabilityAudioProvider"]
