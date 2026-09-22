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
import re
import struct
from collections.abc import Iterable, Iterator
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

_MANIFEST_TAG = b"<mf:manifest>"
_MANIFEST_END_TAG = b"</mf:manifest>"
_XMP_PACKET_PREFIX = (
    b'<?xpacket begin="\xc3\xaf\xc2\xbb\xc2\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
    b' xmlns:mf="https://github.com/backblaze-labs/genblaze/ns/1.0/">'
    b'<rdf:Description rdf:about="">' + _MANIFEST_TAG
)
_XMP_PACKET_SUFFIX = (
    _MANIFEST_END_TAG + b"</rdf:Description></rdf:RDF></x:xmpmeta>" + b'<?xpacket end="w"?>'
)

# Standard XMP-in-JPEG namespace header (XMP spec part 3, §1.1.3).
_XMP_APP1_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_EXIF_APP1_HEADER = b"Exif\x00\x00"
_SOI = b"\xff\xd8"
_APP0, _APP1, _SOS, _EOI = 0xE0, 0xE1, 0xDA, 0xD9
# Markers without a length field (ITU T.81 §B.1.1.3): TEM and RST0-7.
_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
# Segment length is a u16 that counts its own 2 bytes.
_MAX_SEGMENT_PAYLOAD = 0xFFFF - 2
# Real headers carry tens of segments (ICC alone may span up to 255 APP2s);
# the cap bounds CPU on hostile files made of millions of tiny markers.
_MAX_HEADER_SEGMENTS = 65536
_NOT_FF = re.compile(rb"[^\xff]")


def _build_xmp(manifest_json: str) -> bytes:
    """Build an XMP packet containing the manifest JSON (XML-escaped)."""
    # XML-escape manifest JSON to prevent tag injection from prompt content
    escaped = html.escape(manifest_json, quote=False)
    return _XMP_PACKET_PREFIX + escaped.encode("utf-8") + _XMP_PACKET_SUFFIX


def _is_own_packet(data: bytes, start: int, end: int) -> bool:
    """True if ``data[start:end]`` is exactly a packet ``_build_xmp`` wrote.

    Only these are replaced on re-embed. A packet another tool merged our
    manifest into (exiftool, Lightroom) also carries third-party properties,
    so it is kept rather than silently dropped.
    """
    return (
        end - start >= len(_XMP_PACKET_PREFIX) + len(_XMP_PACKET_SUFFIX)
        and data.startswith(_XMP_PACKET_PREFIX, start)
        and data.endswith(_XMP_PACKET_SUFFIX, start, end)
    )


def _manifest_from_packets(data: bytes, packets: Iterable[tuple[int, int]], source: Path) -> str:
    """Return the XML-unescaped manifest JSON from the XMP packets given.

    ``packets`` are ``(start, end)`` offsets of real XMP containers (JPEG
    APP1 payloads, WebP ``XMP `` chunks) — never arbitrary file bytes, so a
    ``<mf:manifest>`` string planted in EXIF, ICC or image data can't shadow
    the embedded one. A packet genblaze wrote wins over one a third-party
    tool merged our manifest into; otherwise the first match is used.
    """
    saw_xmp = False
    fallback: tuple[int, int] | None = None
    for start, end in packets:
        saw_xmp = True
        if _is_own_packet(data, start, end):
            return _decode_manifest(data, start, end, source)
        if fallback is None and data.find(_MANIFEST_TAG, start, end) != -1:
            fallback = (start, end)
    if fallback is not None:
        return _decode_manifest(data, *fallback, source)
    if saw_xmp:
        raise EmbeddingError(f"No genblaze manifest in any XMP packet in {source}")
    raise EmbeddingError(f"No XMP data found in {source}")


def _decode_manifest(data: bytes, start: int, end: int, source: Path) -> str:
    """Decode the ``<mf:manifest>`` element inside ``data[start:end]``.

    Manifest content is XML-escaped, so it can't contain the end tag.
    """
    tag = data.find(_MANIFEST_TAG, start, end)
    content_start = tag + len(_MANIFEST_TAG)
    content_end = data.find(_MANIFEST_END_TAG, content_start, end)
    if content_end == -1:
        raise EmbeddingError(f"Malformed XMP in {source}: unterminated <mf:manifest>")
    if content_end - content_start > MAX_MANIFEST_BYTES:
        raise EmbeddingError(
            f"Embedded manifest exceeds size limit "
            f"({content_end - content_start} > {MAX_MANIFEST_BYTES} bytes)"
        )
    try:
        raw = data[content_start:content_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EmbeddingError(f"Malformed XMP in {source}: manifest is not UTF-8") from exc
    return html.unescape(raw)


def _iter_segments(data: bytes) -> Iterator[tuple[int, int, int, int]]:
    """Yield ``(marker, start, payload_start, end)`` for each header segment.

    ``data[start:end]`` is the verbatim segment including any 0xFF fill bytes
    and the marker/length fields. The last item is the first SOS, yielded as
    ``(_SOS, start, start, start)``: everything from there on — scan headers,
    entropy-coded data, progressive-scan tables, EOI, trailers — is never
    parsed. Offsets (not slices) keep memory flat on large files.
    """
    if not data.startswith(_SOI):
        raise EmbeddingError("Not a valid JPEG (missing SOI marker)")
    pos = 2
    size = len(data)
    for _ in range(_MAX_HEADER_SEGMENTS):
        if pos >= size or data[pos] != 0xFF:
            raise EmbeddingError(f"Malformed JPEG: expected marker at offset {pos}")
        start = pos
        match = _NOT_FF.search(data, pos)  # skip optional fill bytes at C speed
        if match is None:
            raise EmbeddingError("Truncated JPEG: marker runs past end of file")
        pos = match.start()
        marker = data[pos]
        pos += 1
        if marker == _SOS:
            yield _SOS, start, start, start
            return
        if marker == _EOI:
            raise EmbeddingError("Malformed JPEG: no image scan (SOS) before EOI")
        if marker == 0x00:
            raise EmbeddingError(f"Malformed JPEG: stuffed byte outside scan at offset {start}")
        if marker in _STANDALONE_MARKERS:
            yield marker, start, pos, pos
            continue
        if pos + 2 > size:
            raise EmbeddingError("Truncated JPEG: segment length runs past end of file")
        length = struct.unpack_from(">H", data, pos)[0]
        if length < 2 or pos + length > size:
            raise EmbeddingError(f"Truncated JPEG segment 0xFF{marker:02X} at offset {start}")
        yield marker, start, pos + 2, pos + length
        pos += length
    raise EmbeddingError(f"Malformed JPEG: more than {_MAX_HEADER_SEGMENTS} header segments")


def _xmp_packets(data: bytes) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` of the XMP packet in each standard-XMP APP1."""
    for marker, _, payload_start, end in _iter_segments(data):
        if marker == _APP1 and data.startswith(_XMP_APP1_HEADER, payload_start, end):
            yield payload_start + len(_XMP_APP1_HEADER), end


def _embed_xmp_segment(data: bytes, xmp: bytes) -> bytearray:
    """Return JPEG bytes with one genblaze XMP APP1 segment spliced in.

    Segments genblaze wrote (including via the legacy Pillow path) are
    dropped so re-embed replaces. The new segment goes after the leading
    APP0 (JFIF/JFXX) and EXIF APP1 run — both specs require those directly
    after SOI — and ahead of everything else, so it is the first XMP packet.
    """
    payload = _XMP_APP1_HEADER + xmp
    # Defence in depth: MAX_XMP_BYTES already keeps callers well below this.
    if len(payload) > _MAX_SEGMENT_PAYLOAD:
        raise EmbeddingError(
            f"XMP packet too large for one JPEG APP1 segment "
            f"({len(payload)} > {_MAX_SEGMENT_PAYLOAD} bytes). Use sidecar fallback."
        )
    new_segment = b"\xff" + bytes([_APP1]) + struct.pack(">H", len(payload) + 2) + payload

    view = memoryview(data)
    out = bytearray(_SOI)
    inserted = False
    for marker, start, payload_start, end in _iter_segments(data):
        if marker == _SOS:
            if not inserted:
                out += new_segment
            out += view[start:]
            break
        if (
            marker == _APP1
            and data.startswith(_XMP_APP1_HEADER, payload_start, end)
            and _is_own_packet(data, payload_start + len(_XMP_APP1_HEADER), end)
        ):
            continue
        leading = marker == _APP0 or (
            marker == _APP1 and data.startswith(_EXIF_APP1_HEADER, payload_start, end)
        )
        if not inserted and not leading:
            out += new_segment
            inserted = True
        out += view[start:end]
    return out


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
            manifest_json = _manifest_from_packets(data, _xmp_packets(data), source)
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
