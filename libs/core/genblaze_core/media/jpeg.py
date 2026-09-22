"""JPEG media handler — embed/extract manifests via XMP metadata.

The handler splices an XMP ``APP1`` segment into the marker stream rather
than re-encoding through Pillow, so the entropy-coded scan data and every
other segment (EXIF, ICC, quantization/Huffman tables, third-party XMP) stay
byte-identical. Stripping the genblaze segment recovers the original file
exactly, which is what makes the manifest's content binding checkable (#249).
"""

from __future__ import annotations

import html
import json
import os
import struct
from pathlib import Path

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import (
    BaseMediaHandler,
    MediaCapability,
    atomic_write,
    read_media_bytes,
)
from genblaze_core.models.manifest import Manifest, parse_manifest

XMP_NS = "genblaze"
MAX_XMP_BYTES = 60 * 1024  # 60KB size guard

# Opening tag of the genblaze manifest element; its presence marks an XMP
# packet (JPEG APP1 segment or WebP ``XMP `` chunk) as ours.
MANIFEST_TAG = b"<mf:manifest>"
_MANIFEST_END_TAG = b"</mf:manifest>"

# Standard XMP-in-JPEG namespace header (XMP spec part 3, §1.1.3).
_XMP_APP1_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_EXIF_APP1_HEADER = b"Exif\x00\x00"
_SOI = b"\xff\xd8"
_APP0, _APP1, _SOS, _EOI = 0xE0, 0xE1, 0xDA, 0xD9
# Markers without a length field (ITU T.81 §B.1.1.3): TEM and RST0-7.
_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
# Segment length is a u16 that counts its own 2 bytes.
_MAX_SEGMENT_PAYLOAD = 0xFFFF - 2


def _build_xmp(manifest_json: str) -> bytes:
    """Build an XMP packet containing the manifest JSON (XML-escaped)."""
    # XML-escape manifest JSON to prevent tag injection from prompt content
    escaped = html.escape(manifest_json, quote=False)
    xmp = (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        f' xmlns:mf="https://github.com/backblaze-labs/genblaze/ns/1.0/">'
        f'<rdf:Description rdf:about="">'
        f"<mf:manifest>{escaped}</mf:manifest>"
        f"</rdf:Description>"
        f"</rdf:RDF>"
        f"</x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    return xmp.encode("utf-8")


def _split_segments(data: bytes) -> tuple[list[tuple[int, bytes, bytes]], int]:
    """Split a JPEG into header segments and the offset of the first SOS.

    Returns ``([(marker, verbatim_bytes, payload), ...], sos_offset)``.
    ``verbatim_bytes`` includes any 0xFF fill bytes and the marker/length
    fields, so concatenating them (after SOI) reproduces the header exactly.
    Everything from ``sos_offset`` on — scan headers, entropy-coded data,
    further tables for progressive scans, EOI, trailers — is never parsed.
    """
    if data[:2] != _SOI:
        raise EmbeddingError("Not a valid JPEG (missing SOI marker)")
    segments: list[tuple[int, bytes, bytes]] = []
    pos = 2
    size = len(data)
    while True:
        if pos >= size or data[pos] != 0xFF:
            raise EmbeddingError(f"Malformed JPEG: expected marker at offset {pos}")
        start = pos
        while pos < size and data[pos] == 0xFF:  # optional fill bytes
            pos += 1
        if pos >= size:
            raise EmbeddingError("Truncated JPEG: marker runs past end of file")
        marker = data[pos]
        pos += 1
        if marker == _SOS:
            return segments, start
        if marker == _EOI:
            raise EmbeddingError("Malformed JPEG: no image scan (SOS) before EOI")
        if marker == 0x00:
            raise EmbeddingError(f"Malformed JPEG: stuffed byte outside scan at offset {start}")
        if marker in _STANDALONE_MARKERS:
            segments.append((marker, data[start:pos], b""))
            continue
        if pos + 2 > size:
            raise EmbeddingError("Truncated JPEG: segment length runs past end of file")
        length = struct.unpack(">H", data[pos : pos + 2])[0]
        if length < 2 or pos + length > size:
            raise EmbeddingError(f"Truncated JPEG segment 0xFF{marker:02X} at offset {start}")
        segments.append((marker, data[start : pos + length], data[pos + 2 : pos + length]))
        pos += length


def _is_genblaze_app1(marker: int, payload: bytes) -> bool:
    return marker == _APP1 and payload.startswith(_XMP_APP1_HEADER) and MANIFEST_TAG in payload


def _embed_xmp_segment(data: bytes, xmp: bytes) -> bytes:
    """Return JPEG bytes with one genblaze XMP APP1 segment spliced in.

    Existing genblaze segments (including ones written by the legacy Pillow
    path) are dropped so re-embed replaces. The new segment goes after the
    leading APP0 (JFIF/JFXX) and EXIF APP1 run — both specs require those to
    sit directly after SOI — and ahead of everything else.
    """
    payload = _XMP_APP1_HEADER + xmp
    if len(payload) > _MAX_SEGMENT_PAYLOAD:
        raise EmbeddingError(
            f"XMP packet too large for one JPEG APP1 segment "
            f"({len(payload)} > {_MAX_SEGMENT_PAYLOAD} bytes). Use sidecar fallback."
        )
    new_segment = b"\xff" + bytes([_APP1]) + struct.pack(">H", len(payload) + 2) + payload

    segments, sos_offset = _split_segments(data)
    out = bytearray(_SOI)
    inserted = False
    for marker, raw, seg_payload in segments:
        if _is_genblaze_app1(marker, seg_payload):
            continue
        leading = marker == _APP0 or (
            marker == _APP1 and seg_payload.startswith(_EXIF_APP1_HEADER)
        )
        if not inserted and not leading:
            out += new_segment
            inserted = True
        out += raw
    if not inserted:
        out += new_segment
    out += data[sos_offset:]
    return bytes(out)


def _scan_xmp_for_manifest(data: bytes, source: Path) -> str:
    """Return the XML-unescaped manifest JSON from the genblaze XMP element.

    JPEG/WebP files can carry several XMP packets — Photoshop, Lightroom or
    any other tool may have written its own before genblaze embedded — and
    third-party packets don't always carry an ``<?xpacket end?>`` wrapper.
    Searching for the ``<mf:manifest>`` element directly (rather than
    walking packet boundaries) finds ours wherever it sits, in one linear
    pass. Manifest content is XML-escaped, so it can't contain the end tag.
    """
    start = data.find(MANIFEST_TAG)
    if start == -1:
        if b"<x:xmpmeta" in data:
            raise EmbeddingError(f"No genblaze manifest in any XMP packet in {source}")
        raise EmbeddingError(f"No XMP data found in {source}")
    content_start = start + len(MANIFEST_TAG)
    end = data.find(_MANIFEST_END_TAG, content_start)
    if end == -1:
        raise EmbeddingError(f"Malformed XMP in {source}: unterminated <mf:manifest>")
    if end - content_start > MAX_MANIFEST_BYTES:
        raise EmbeddingError(
            f"Embedded manifest exceeds size limit "
            f"({end - content_start} > {MAX_MANIFEST_BYTES} bytes)"
        )
    try:
        raw = data[content_start:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EmbeddingError(f"Malformed XMP in {source}: manifest is not UTF-8") from exc
    return html.unescape(raw)


class JpegHandler(BaseMediaHandler):
    """Embed and extract manifests in JPEG XMP metadata."""

    def embed(
        self,
        source: str | os.PathLike[str],
        manifest: Manifest,
        output: str | os.PathLike[str] | None = None,
    ) -> Path:
        try:
            # Coerce both source= and output= — either being a bare str
            # would leak into the -> Path contract below (#225).
            source = Path(source)
            output = Path(output) if output else source
            manifest_json = manifest.to_canonical_json()
            xmp_data = _build_xmp(manifest_json)
            if len(xmp_data) > MAX_XMP_BYTES:
                raise EmbeddingError(
                    f"Manifest too large for JPEG XMP ({len(xmp_data)} bytes > {MAX_XMP_BYTES}). "
                    "Use sidecar fallback."
                )
            new_data = _embed_xmp_segment(read_media_bytes(source), xmp_data)
            with atomic_write(output) as tmp:
                tmp.write_bytes(new_data)
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in JPEG: {exc}") from exc

    def extract(self, source: str | os.PathLike[str]) -> Manifest:
        try:
            source = Path(source)
            data = read_media_bytes(source)
            manifest_json = _scan_xmp_for_manifest(data, source)
            return parse_manifest(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from JPEG: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["image/jpeg"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="image/jpeg",
                max_payload_bytes=MAX_XMP_BYTES,
                metadata_location="XMP",
                strip_risk="medium",
            )
        ]
