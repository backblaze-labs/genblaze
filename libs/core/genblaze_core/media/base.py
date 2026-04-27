"""Base media handler ABC."""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.models.manifest import Manifest

# Max file size for in-memory processing (500 MB)
MAX_FILE_BYTES = 500 * 1024 * 1024

# Max file size for mmap-based processing (2 GB) — used by MP4 handler
MAX_MMAP_BYTES = 2 * 1024 * 1024 * 1024


def read_media_bytes(source: Path) -> bytes:
    """Read a media file with size limit to prevent OOM on malicious input."""
    file_size = source.stat().st_size
    if file_size > MAX_FILE_BYTES:
        raise EmbeddingError(
            f"File too large for in-memory processing ({file_size} bytes, limit {MAX_FILE_BYTES})"
        )
    return source.read_bytes()


@contextmanager
def atomic_write(target: Path) -> Iterator[Path]:
    """Yield a temp path in target's parent dir; on success rename to target.

    On exception, the temp file is unlinked and the original target is left
    untouched. Same-filesystem temp guarantees ``os.replace`` is atomic.

    Usage::

        with atomic_write(out_path) as tmp:
            tmp.write_bytes(data)              # bytes
            # or pass tmp to a library that writes by path:
            img.save(tmp, "JPEG", ...)
            tags.save(tmp)
    """
    target = Path(target)
    fd, tmp_str = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_str)
    try:
        yield tmp
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@dataclass
class MediaCapability:
    """Describes a handler's capability for a specific media type."""

    mime_type: str
    max_payload_bytes: int | None
    metadata_location: str
    strip_risk: str  # "low", "medium", "high"


class BaseMediaHandler(ABC):
    """Abstract base for media embedding/extraction."""

    @abstractmethod
    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        """Embed a manifest into a media file. Returns path to output file."""
        ...

    @abstractmethod
    def extract(self, source: Path) -> Manifest:
        """Extract a manifest from a media file."""
        ...

    def verify(self, source: Path) -> bool:
        """Extract and verify the manifest hash. Returns True if valid."""
        manifest = self.extract(source)
        return manifest.verify()

    @staticmethod
    @abstractmethod
    def capabilities() -> list[str]:
        """Return supported media types (e.g. ['image/png'])."""
        ...

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        """Return detailed capabilities. Override in subclasses."""
        return []
