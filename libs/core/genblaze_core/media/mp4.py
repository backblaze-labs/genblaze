"""MP4 media handler — embed/extract manifests via a custom UUID box.

For files under 500 MB, uses in-memory processing (fast).
For files 500 MB–2 GB, uses seek-based file I/O to avoid loading entire file into RAM.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import uuid
from pathlib import Path

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import (
    MAX_FILE_BYTES,
    MAX_MMAP_BYTES,
    BaseMediaHandler,
    MediaCapability,
)
from genblaze_core.models.manifest import Manifest

# Custom UUID for genblaze manifest box
GENBLAZE_UUID = uuid.UUID("6d6f6461-6c66-6c6f-7700-000000000001")
GENBLAZE_UUID_BYTES = GENBLAZE_UUID.bytes


class Mp4Handler(BaseMediaHandler):
    """Embed and extract manifests in MP4 using a custom UUID box."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        file_size = source.stat().st_size
        try:
            if file_size <= MAX_FILE_BYTES:
                return self._embed_inmemory(source, manifest, output)
            if file_size <= MAX_MMAP_BYTES:
                return self._embed_streaming(source, manifest, output)
            raise EmbeddingError(
                f"File too large for MP4 embedding ({file_size} bytes, limit {MAX_MMAP_BYTES}). "
                "Use sidecar fallback."
            )
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in MP4: {exc}") from exc

    def _embed_inmemory(self, source: Path, manifest: Manifest, output: Path) -> Path:
        """In-memory embed for small files (< 500 MB).

        Appends the UUID box at EOF rather than inserting before mdat.
        Fast-start MP4 files (ftyp | moov | mdat) carry absolute file
        offsets in moov.stco / co64 pointing into mdat — shifting mdat
        by the UUID-box size would invalidate every sample offset and
        break playback. Appending at EOF leaves every pre-existing byte
        at its original position.
        """
        data = source.read_bytes()
        if not _is_mp4(data):
            raise EmbeddingError(f"Not a valid MP4 file: {source}")

        manifest_bytes = manifest.to_canonical_json().encode("utf-8")
        box = _build_uuid_box(manifest_bytes)

        cleaned = _remove_genblaze_box(data)
        result = cleaned + box

        _atomic_write_bytes(Path(output), result)
        return output

    def _embed_streaming(self, source: Path, manifest: Manifest, output: Path) -> Path:
        """Seek-based embed for large files (500 MB–2 GB).

        Copies every non-genblaze top-level box verbatim and appends our
        UUID box at EOF. Same invariant as _embed_inmemory: never shift
        any pre-existing byte, so moov stco/co64 sample offsets stay
        valid and playback is preserved.
        """
        with open(source, "rb") as f:
            header = f.read(8)
            if len(header) < 8 or header[4:8] != b"ftyp":
                raise EmbeddingError(f"Not a valid MP4 file: {source}")

        manifest_bytes = manifest.to_canonical_json().encode("utf-8")
        uuid_box = _build_uuid_box(manifest_bytes)

        fd, tmp = tempfile.mkstemp(dir=Path(output).parent, suffix=".tmp")
        os.close(fd)
        try:
            with open(source, "rb") as src, open(tmp, "wb") as dst:
                file_size = source.stat().st_size
                pos = 0

                while pos < file_size - 8:
                    src.seek(pos)
                    hdr = src.read(8)
                    if len(hdr) < 8:
                        break
                    size_32 = struct.unpack(">I", hdr[:4])[0]
                    box_type = hdr[4:8]

                    # Handle extended (64-bit) size and size==0 (extends to EOF)
                    if size_32 == 1:
                        ext = src.read(8)
                        if len(ext) < 8:
                            break
                        box_size = struct.unpack(">Q", ext)[0]
                    elif size_32 == 0:
                        box_size = file_size - pos
                    else:
                        box_size = size_32

                    if box_size < 8 or pos + box_size > file_size:
                        src.seek(pos)
                        _copy_chunks(src, dst, file_size - pos)
                        break

                    # Skip existing genblaze UUID boxes — we re-append ours
                    # at EOF below so every re-embed produces exactly one.
                    if box_type == b"uuid" and box_size > 24:
                        src.seek(pos + 8)
                        box_uuid = src.read(16)
                        if box_uuid == GENBLAZE_UUID_BYTES:
                            pos += box_size
                            continue

                    src.seek(pos)
                    _copy_chunks(src, dst, box_size)
                    pos += box_size

                # Append UUID at EOF (after mdat) so moov offsets stay valid.
                dst.write(uuid_box)

            os.replace(tmp, output)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return output

    def extract(self, source: Path) -> Manifest:
        try:
            file_size = source.stat().st_size
            if file_size <= MAX_FILE_BYTES:
                data = source.read_bytes()
                if not _is_mp4(data):
                    raise EmbeddingError(f"Not a valid MP4 file: {source}")
                manifest_json = _find_genblaze_box(data)
            else:
                # Streaming extract: scan box headers without loading full file
                manifest_json = self._extract_streaming(source, file_size)

            if manifest_json is None:
                raise EmbeddingError(f"No genblaze manifest found in {source}")
            return Manifest.model_validate(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from MP4: {exc}") from exc

    def _extract_streaming(self, source: Path, file_size: int) -> bytes | None:
        """Seek-based box scan for large files — reads only box headers and UUID payloads."""
        with open(source, "rb") as f:
            header = f.read(8)
            if len(header) < 8 or header[4:8] != b"ftyp":
                raise EmbeddingError(f"Not a valid MP4 file: {source}")
            f.seek(0)

            pos = 0
            while pos < file_size:
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break
                raw_size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8]
                header_size = 8

                if raw_size == 1:
                    # 64-bit extended size
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    box_size = struct.unpack(">Q", ext)[0]
                    header_size = 16
                else:
                    box_size = raw_size

                if box_size == 0:
                    box_size = file_size - pos

                if box_size < 8:
                    break

                if box_type == b"uuid":
                    uuid_bytes = f.read(16)
                    if uuid_bytes == GENBLAZE_UUID_BYTES:
                        payload_size = box_size - header_size - 16
                        return f.read(payload_size)

                pos += box_size
        return None

    @staticmethod
    def capabilities() -> list[str]:
        return ["video/mp4"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="video/mp4",
                max_payload_bytes=None,
                metadata_location="UUID box",
                strip_risk="low",
            )
        ]


# --- Chunk copy for streaming ---

_COPY_CHUNK = 256 * 1024  # 256 KB


def _copy_chunks(src, dst, nbytes: int) -> None:
    """Copy exactly nbytes from src to dst in chunks."""
    remaining = nbytes
    while remaining > 0:
        chunk = src.read(min(_COPY_CHUNK, remaining))
        if not chunk:
            break
        dst.write(chunk)
        remaining -= len(chunk)


# --- Shared helpers ---


def _build_uuid_box(manifest_bytes: bytes) -> bytes:
    """Build a genblaze UUID box from manifest JSON bytes."""
    box_payload = GENBLAZE_UUID_BYTES + manifest_bytes
    box_total = 8 + len(box_payload)
    if box_total > 0xFFFFFFFF:
        raise EmbeddingError(
            f"Manifest too large for MP4 UUID box ({box_total} bytes). Use sidecar fallback."
        )
    return struct.pack(">I", box_total) + b"uuid" + box_payload


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically via temp file + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    fd_closed = False
    try:
        os.write(fd, data)
        os.close(fd)
        fd_closed = True
        os.replace(tmp, path)
    except BaseException:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_box_size(data: bytes, pos: int) -> int:
    """Read MP4 box size, handling extended (64-bit) and size==0 (extends to EOF)."""
    size_32 = struct.unpack(">I", data[pos : pos + 4])[0]
    if size_32 == 1 and pos + 16 <= len(data):
        return struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
    if size_32 == 0:
        return len(data) - pos  # Box extends to end of data
    return size_32


def _is_mp4(data: bytes) -> bool:
    """Check if data looks like an MP4 file (ftyp box check)."""
    if len(data) < 8:
        return False
    return data[4:8] == b"ftyp"


def _find_genblaze_box(data: bytes) -> bytes | None:
    """Find and extract manifest payload from a genblaze UUID box."""
    pos = 0
    while pos < len(data) - 8:
        box_size = _read_box_size(data, pos)
        if box_size < 8 or box_size > len(data) - pos:
            break
        box_type = data[pos + 4 : pos + 8]
        if box_type == b"uuid" and box_size > 24:
            box_uuid = data[pos + 8 : pos + 24]
            if box_uuid == GENBLAZE_UUID_BYTES:
                return data[pos + 24 : pos + box_size]
        pos += box_size
    return None


def _remove_genblaze_box(data: bytes) -> bytes:
    """Remove existing genblaze UUID boxes from MP4 data."""
    result = bytearray()
    pos = 0
    while pos < len(data) - 8:
        box_size = _read_box_size(data, pos)
        if box_size < 8 or box_size > len(data) - pos:
            result.extend(data[pos:])
            break
        box_type = data[pos + 4 : pos + 8]
        is_genblaze = (
            box_type == b"uuid"
            and box_size > 24
            and data[pos + 8 : pos + 24] == GENBLAZE_UUID_BYTES
        )
        if not is_genblaze:
            result.extend(data[pos : pos + box_size])
        pos += box_size
    return bytes(result)
