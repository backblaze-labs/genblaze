"""Media handlers for manifest embedding/extraction."""

from __future__ import annotations

from genblaze_core.media.base import BaseMediaHandler, MediaCapability
from genblaze_core.media.embedder import EmbedResult, SmartEmbedder, guess_mime
from genblaze_core.media.jpeg import JpegHandler
from genblaze_core.media.mp4 import Mp4Handler
from genblaze_core.media.png import PngHandler
from genblaze_core.media.sidecar import SidecarHandler
from genblaze_core.media.webp import WebpHandler

__all__ = [
    "AacHandler",
    "BaseMediaHandler",
    "EmbedResult",
    "FlacHandler",
    "JpegHandler",
    "MediaCapability",
    "Mp3Handler",
    "Mp4Handler",
    "PngHandler",
    "SidecarHandler",
    "SmartEmbedder",
    "WavHandler",
    "WebpHandler",
    "get_handler",
    "guess_mime",
]


def __getattr__(name: str):
    """Lazy-load audio handlers to avoid hard dependency on mutagen."""
    _lazy = {
        "AacHandler": ("genblaze_core.media.aac", "AacHandler"),
        "FlacHandler": ("genblaze_core.media.flac", "FlacHandler"),
        "Mp3Handler": ("genblaze_core.media.mp3", "Mp3Handler"),
        "WavHandler": ("genblaze_core.media.wav", "WavHandler"),
    }
    if name in _lazy:
        import importlib

        mod = importlib.import_module(_lazy[name][0])
        val = getattr(mod, _lazy[name][1])
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_HANDLER_REGISTRY: dict[str, type[BaseMediaHandler]] = {
    "image/png": PngHandler,
    "image/jpeg": JpegHandler,
    "image/webp": WebpHandler,
    "video/mp4": Mp4Handler,
}

# Audio handlers require mutagen — register lazily to avoid hard dependency
_LAZY_HANDLERS: dict[str, tuple[str, str]] = {
    "audio/aac": ("genblaze_core.media.aac", "AacHandler"),
    "audio/mp4": ("genblaze_core.media.aac", "AacHandler"),
    "audio/x-m4a": ("genblaze_core.media.aac", "AacHandler"),
    "audio/flac": ("genblaze_core.media.flac", "FlacHandler"),
    "audio/mpeg": ("genblaze_core.media.mp3", "Mp3Handler"),
    "audio/wav": ("genblaze_core.media.wav", "WavHandler"),
}


def get_handler(mime_type: str) -> BaseMediaHandler | None:
    """Get a handler instance for the given MIME type, or None if unsupported."""
    handler_cls = _HANDLER_REGISTRY.get(mime_type)
    if handler_cls is not None:
        return handler_cls()

    # Try lazy-loaded handlers (audio formats requiring mutagen)
    lazy = _LAZY_HANDLERS.get(mime_type)
    if lazy is not None:
        import importlib

        mod = importlib.import_module(lazy[0])
        cls = getattr(mod, lazy[1])
        return cls()

    return None
