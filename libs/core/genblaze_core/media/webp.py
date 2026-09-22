"""WebP media handler — embed/extract manifests via XMP metadata.

The handler splices an ``XMP `` chunk into the RIFF container rather than
re-encoding through Pillow, so the VP8/VP8L bitstream (and ALPH, ICCP, EXIF,
ANIM/ANMF and unknown chunks) stay byte-identical (#249). Simple-format files
(a lone ``VP8 ``/``VP8L`` chunk) are promoted to the extended format by
prepending a ``VP8X`` header, which is the only way WebP can carry metadata.
"""

from __future__ import annotations

import json
import os
import struct
import warnings
from collections.abc import Iterator
from pathlib import Path

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import (
    BaseMediaHandler,
    MediaCapability,
    atomic_write,
    read_media_bytes,
)
from genblaze_core.media.jpeg import (
    MAX_XMP_BYTES,
    _build_xmp,
    _is_own_packet,
    _manifest_from_packets,
)
from genblaze_core.models.manifest import Manifest, parse_manifest

# VP8X feature flags (WebP container spec, "Extended File Format").
_VP8X_XMP_FLAG = 0x04
_VP8X_ALPHA_FLAG = 0x10
_VP8X_PAYLOAD_SIZE = 10
_MAX_RIFF_SIZE = 0xFFFFFFFF
# Generous for long animations (one ANMF chunk per frame) while bounding CPU
# on hostile files made of millions of empty 8-byte chunks.
_MAX_CHUNKS = 1 << 20


def _chunk(fourcc: bytes, payload: bytes) -> bytes:
    """Serialize a RIFF chunk, padding odd-length payloads to a word boundary."""
    return fourcc + struct.pack("<I", len(payload)) + payload + b"\x00" * (len(payload) & 1)


def _riff_end(data: bytes) -> int:
    """Validate the RIFF/WEBP header and return the RIFF-declared end offset."""
    if len(data) < 12 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        raise EmbeddingError("Not a valid WebP (missing RIFF/WEBP header)")
    riff_end = 8 + struct.unpack_from("<I", data, 4)[0]
    if riff_end > len(data):
        raise EmbeddingError("Truncated WebP: RIFF size exceeds file length")
    return riff_end


def _iter_chunks(data: bytes, riff_end: int) -> Iterator[tuple[bytes, int, int, int, int]]:
    """Yield ``(fourcc, start, payload_start, payload_end, end)`` per chunk.

    ``data[start:end]`` is the verbatim chunk including its pad byte. Offsets
    (not slices) keep memory flat — the codec chunk is nearly the whole file.
    """
    pos = 12
    count = 0
    while pos < riff_end:
        count += 1
        if count > _MAX_CHUNKS:
            raise EmbeddingError(f"Malformed WebP: more than {_MAX_CHUNKS} chunks")
        if pos + 8 > riff_end:
            raise EmbeddingError(f"Truncated WebP chunk header at offset {pos}")
        fourcc = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        end = pos + 8 + size + (size & 1)
        if end > riff_end:
            raise EmbeddingError(
                f"Truncated WebP chunk {fourcc.decode('ascii', 'replace')!r} at offset {pos}"
            )
        yield fourcc, pos, pos + 8, pos + 8 + size, end
        pos = end


def _simple_canvas(fourcc: bytes, head: bytes) -> tuple[int, int, bool]:
    """Read ``(width, height, has_alpha)`` from a simple-format codec chunk.

    ``head`` is the start of the chunk payload (the first 16 bytes suffice).
    """
    if fourcc == b"VP8L":
        # 1-byte signature 0x2F, then 14-bit width-1, 14-bit height-1, alpha bit.
        if len(head) < 5 or head[0] != 0x2F:
            raise EmbeddingError("Malformed VP8L header")
        bits = struct.unpack_from("<I", head, 1)[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool((bits >> 28) & 1)
    # VP8 key frame: 3-byte frame tag, start code 9D 01 2A, then 14-bit
    # width/height (top 2 bits are scaling). Simple-format VP8 has no alpha.
    if len(head) < 10 or head[0] & 1 or head[3:6] != b"\x9d\x01\x2a":
        raise EmbeddingError("Malformed VP8 header (not a key frame)")
    width = struct.unpack_from("<H", head, 6)[0] & 0x3FFF
    height = struct.unpack_from("<H", head, 8)[0] & 0x3FFF
    if not width or not height:
        raise EmbeddingError("Malformed VP8 header (zero canvas dimension)")
    return width, height, False


def _xmp_packets(data: bytes) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` of the payload of each ``XMP `` chunk."""
    for fourcc, _, payload_start, payload_end, _ in _iter_chunks(data, _riff_end(data)):
        if fourcc == b"XMP ":
            yield payload_start, payload_end


def _embed_xmp_chunk(data: bytes, xmp: bytes) -> bytearray:
    """Return WebP bytes with one genblaze ``XMP `` chunk appended.

    Chunks genblaze wrote (including legacy Pillow-written ones) are dropped
    so re-embed replaces; third-party XMP chunks are kept. The new chunk is
    appended last, which satisfies the spec's "metadata after image data"
    ordering for still and animated files alike.
    """
    riff_end = _riff_end(data)
    chunks = _iter_chunks(data, riff_end)
    first = next(chunks, None)
    if first is None:
        raise EmbeddingError("Malformed WebP: RIFF container has no chunks")
    fourcc, start, payload_start, payload_end, end = first

    view = memoryview(data)
    out = bytearray(b"RIFF\x00\x00\x00\x00WEBP")  # size patched below
    if fourcc == b"VP8X":
        if payload_end - payload_start < _VP8X_PAYLOAD_SIZE:
            raise EmbeddingError("Malformed VP8X header")
        # Only the flags byte (first payload byte) changes; the rest is kept.
        out += view[start:end]
        out[12 + 8] |= _VP8X_XMP_FLAG
    elif fourcc in (b"VP8 ", b"VP8L"):
        head = data[payload_start : min(payload_end, payload_start + 16)]
        width, height, alpha = _simple_canvas(fourcc, head)
        flags = _VP8X_XMP_FLAG | (_VP8X_ALPHA_FLAG if alpha else 0)
        vp8x = (
            bytes([flags, 0, 0, 0])
            + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little")
        )
        out += _chunk(b"VP8X", vp8x)
        out += view[start:end]  # the codec chunk itself is kept verbatim
    else:
        raise EmbeddingError(
            f"Unsupported WebP layout: first chunk {fourcc.decode('ascii', 'replace')!r}"
        )

    for fourcc, start, payload_start, payload_end, end in chunks:
        if fourcc == b"XMP " and _is_own_packet(data, payload_start, payload_end):
            continue
        out += view[start:end]
    out += _chunk(b"XMP ", xmp)
    riff_size = len(out) - 8
    if riff_size > _MAX_RIFF_SIZE:
        raise EmbeddingError("WebP too large for a RIFF container after embedding")
    struct.pack_into("<I", out, 4, riff_size)
    out += view[riff_end:]  # bytes trailing the RIFF end are carried through
    return out


class WebpHandler(BaseMediaHandler):
    """Embed and extract manifests in WebP XMP metadata."""

    def embed(
        self,
        source: str | os.PathLike[str],
        manifest: Manifest,
        output: str | os.PathLike[str] | None = None,
        *,
        lossless: bool | None = None,
        quality: int | None = None,
    ) -> Path:
        """Embed manifest into a WebP file without re-encoding the image.

        ``lossless`` and ``quality`` are deprecated no-ops: they configured a
        Pillow re-encode that no longer happens, since the source bitstream
        is always preserved verbatim.
        """
        if lossless is not None or quality is not None:
            warnings.warn(
                "WebpHandler.embed(lossless=..., quality=...) is deprecated since "
                "genblaze-core 0.3.9 and ignored: embedding no longer re-encodes, so "
                "the source bitstream is always preserved. The parameters will be "
                "removed in genblaze-core 0.4.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        try:
            # Coerce both source= and output= — either being a bare str
            # would leak into the -> Path contract below (#225).
            source = Path(source)
            output = Path(output) if output else source
            manifest_json = manifest.to_canonical_json()
            xmp_data = _build_xmp(manifest_json)
            if len(xmp_data) > MAX_XMP_BYTES:
                raise EmbeddingError(
                    f"Manifest too large for WebP XMP ({len(xmp_data)} bytes > {MAX_XMP_BYTES}). "
                    "Use sidecar fallback."
                )
            new_data = _embed_xmp_chunk(read_media_bytes(source), xmp_data)
            with atomic_write(output) as tmp:
                tmp.write_bytes(new_data)
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in WebP: {exc}") from exc

    def extract(self, source: str | os.PathLike[str]) -> Manifest:
        try:
            source = Path(source)
            data = read_media_bytes(source)
            manifest_json = _manifest_from_packets(data, _xmp_packets(data), source)
            return parse_manifest(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from WebP: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["image/webp"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="image/webp",
                max_payload_bytes=MAX_XMP_BYTES,
                metadata_location="XMP",
                strip_risk="medium",
            )
        ]
