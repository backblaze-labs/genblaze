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
from pathlib import Path

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import (
    BaseMediaHandler,
    MediaCapability,
    atomic_write,
    read_media_bytes,
)
from genblaze_core.media.jpeg import (
    MANIFEST_TAG,
    MAX_XMP_BYTES,
    _build_xmp,
    _scan_xmp_for_manifest,
)
from genblaze_core.models.manifest import Manifest, parse_manifest

# VP8X feature flags (WebP container spec, "Extended File Format").
_VP8X_XMP_FLAG = 0x04
_VP8X_ALPHA_FLAG = 0x10
_VP8X_PAYLOAD_SIZE = 10
_MAX_RIFF_SIZE = 0xFFFFFFFF


def _chunk(fourcc: bytes, payload: bytes) -> bytes:
    """Serialize a RIFF chunk, padding odd-length payloads to a word boundary."""
    return fourcc + struct.pack("<I", len(payload)) + payload + b"\x00" * (len(payload) & 1)


def _split_chunks(data: bytes) -> tuple[list[tuple[bytes, bytes, bytes]], bytes]:
    """Split a WebP file into ``[(fourcc, payload, verbatim_bytes), ...]``.

    Also returns any bytes trailing the RIFF-declared end so they can be
    carried through unchanged. Raises ``EmbeddingError`` on bad magic or any
    chunk that runs past the RIFF end.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise EmbeddingError("Not a valid WebP (missing RIFF/WEBP header)")
    riff_end = 8 + struct.unpack("<I", data[4:8])[0]
    if riff_end > len(data):
        raise EmbeddingError("Truncated WebP: RIFF size exceeds file length")
    chunks: list[tuple[bytes, bytes, bytes]] = []
    pos = 12
    while pos < riff_end:
        if pos + 8 > riff_end:
            raise EmbeddingError(f"Truncated WebP chunk header at offset {pos}")
        fourcc = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        end = pos + 8 + size + (size & 1)
        if end > riff_end:
            raise EmbeddingError(
                f"Truncated WebP chunk {fourcc.decode('ascii', 'replace')!r} at offset {pos}"
            )
        chunks.append((fourcc, data[pos + 8 : pos + 8 + size], data[pos:end]))
        pos = end
    if not chunks:
        raise EmbeddingError("Malformed WebP: RIFF container has no chunks")
    return chunks, data[riff_end:]


def _simple_canvas(fourcc: bytes, payload: bytes) -> tuple[int, int, bool]:
    """Read ``(width, height, has_alpha)`` from a simple-format codec chunk."""
    if fourcc == b"VP8L":
        # 1-byte signature 0x2F, then 14-bit width-1, 14-bit height-1, alpha bit.
        if len(payload) < 5 or payload[0] != 0x2F:
            raise EmbeddingError("Malformed VP8L header")
        bits = struct.unpack("<I", payload[1:5])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool((bits >> 28) & 1)
    # VP8 key frame: 3-byte frame tag, start code 9D 01 2A, then 14-bit
    # width/height (top 2 bits are scaling). Simple-format VP8 has no alpha.
    if len(payload) < 10 or payload[0] & 1 or payload[3:6] != b"\x9d\x01\x2a":
        raise EmbeddingError("Malformed VP8 header (not a key frame)")
    width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
    height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
    if not width or not height:
        raise EmbeddingError("Malformed VP8 header (zero canvas dimension)")
    return width, height, False


def _embed_xmp_chunk(data: bytes, xmp: bytes) -> bytes:
    """Return WebP bytes with one genblaze ``XMP `` chunk appended.

    Existing genblaze XMP chunks (including legacy Pillow-written ones) are
    dropped so re-embed replaces; third-party XMP chunks are kept. The new
    chunk is appended last, which satisfies the spec's "metadata after image
    data" ordering for still and animated files alike.
    """
    chunks, trailer = _split_chunks(data)
    first_fourcc, first_payload, _ = chunks[0]

    if first_fourcc == b"VP8X":
        if len(first_payload) < _VP8X_PAYLOAD_SIZE:
            raise EmbeddingError("Malformed VP8X header")
        # Only the flags byte changes; reserved bytes and canvas size are kept.
        header = _chunk(b"VP8X", bytes([first_payload[0] | _VP8X_XMP_FLAG]) + first_payload[1:])
        rest = chunks[1:]
    elif first_fourcc in (b"VP8 ", b"VP8L"):
        width, height, alpha = _simple_canvas(first_fourcc, first_payload)
        flags = _VP8X_XMP_FLAG | (_VP8X_ALPHA_FLAG if alpha else 0)
        vp8x = (
            bytes([flags, 0, 0, 0])
            + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little")
        )
        header = _chunk(b"VP8X", vp8x)
        rest = chunks  # the codec chunk itself is kept verbatim
    else:
        raise EmbeddingError(
            f"Unsupported WebP layout: first chunk {first_fourcc.decode('ascii', 'replace')!r}"
        )

    body = bytearray(b"WEBP")
    body += header
    for fourcc, payload, raw in rest:
        if fourcc == b"XMP " and MANIFEST_TAG in payload:
            continue
        body += raw
    body += _chunk(b"XMP ", xmp)
    if len(body) > _MAX_RIFF_SIZE:
        raise EmbeddingError("WebP too large for a RIFF container after embedding")
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body) + trailer


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
                "WebpHandler.embed(lossless=..., quality=...) is deprecated and ignored: "
                "embedding no longer re-encodes, so the source bitstream is always "
                "preserved. Drop the arguments.",
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
            manifest_json = _scan_xmp_for_manifest(data, source)
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
