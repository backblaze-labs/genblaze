"""AssemblyAI (speech-to-text / transcription) provider adapter for genblaze."""

from genblaze_assemblyai.provider import AssemblyAIProvider

from ._version import __version__  # noqa: F401 — re-exported

__all__ = ["AssemblyAIProvider"]
