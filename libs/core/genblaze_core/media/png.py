"""PNG media handler — embed/extract manifests via iTXt chunks.

The handler patches PNG bytes directly at the chunk level rather than
re-encoding through Pillow. This preserves every ancillary chunk verbatim
(``pHYs``, ``gAMA``, ``cHRM``, ``sRGB``, ``bKGD``, ``tIME``, ``iCCP``, plus
any private chunks) and avoids decoding pixel data, which is also faster
for large images.
"""

from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, atomic_write, read_media_bytes
from genblaze_core.models.manifest import Manifest

ITXT_KEY = "genblaze:manifest"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _build_itxt(keyword: str, text: str) -> bytes:
    """Build a PNG iTXt chunk (uncompressed) carrying our manifest text.

    iTXt body layout (PNG spec §11.3.4.5):
        keyword \\x00 compression_flag(1) compression_method(1)
        language_tag \\x00 translated_keyword \\x00 text
    Compression flag 0 means the text is stored verbatim.
    """
    body = (
        keyword.encode("latin-1")  # keyword
        + b"\x00"
        + b"\x00"  # compression flag (0 = uncompressed)
        + b"\x00"  # compression method (ignored when uncompressed)
        + b"\x00"  # language tag (empty) + null
        + b"\x00"  # translated keyword (empty) + null
        + text.encode("utf-8")  # text — NOT null-terminated
    )
    chunk_type = b"iTXt"
    length = struct.pack(">I", len(body))
    crc = struct.pack(">I", zlib.crc32(chunk_type + body) & 0xFFFFFFFF)
    return length + chunk_type + body + crc


def _walk_chunks(data: bytes) -> Iterator[tuple[int, bytes, int]]:
    """Yield ``(start, chunk_type, total_size)`` for each chunk in a PNG.

    ``total_size`` includes 4-byte length + 4-byte type + payload + 4-byte CRC,
    so ``data[start : start + total_size]`` is the verbatim chunk bytes.
    Raises ``EmbeddingError`` on bad signature or truncated chunks.
    """
    if data[: len(_PNG_SIGNATURE)] != _PNG_SIGNATURE:
        raise EmbeddingError("Not a valid PNG (signature mismatch)")
    pos = len(_PNG_SIGNATURE)
    while pos < len(data):
        if pos + 12 > len(data):
            raise EmbeddingError("Truncated PNG chunk header")
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        total = 12 + length
        if pos + total > len(data):
            raise EmbeddingError(f"Truncated PNG chunk {chunk_type.decode('ascii', 'replace')}")
        yield pos, chunk_type, total
        pos += total


def _itxt_keyword(payload: bytes) -> str | None:
    """Read the keyword field from an iTXt payload, or None if malformed."""
    null_pos = payload.find(b"\x00")
    if null_pos == -1 or null_pos > 79:
        return None
    return payload[:null_pos].decode("latin-1", errors="replace")


def _itxt_text(payload: bytes) -> str:
    """Decode the text field from an uncompressed iTXt payload."""
    null_pos = payload.find(b"\x00")
    if null_pos == -1:
        raise EmbeddingError("Malformed iTXt: missing keyword terminator")
    cursor = null_pos + 1
    if cursor + 2 > len(payload):
        raise EmbeddingError("Malformed iTXt: missing compression bytes")
    if payload[cursor] != 0:
        raise EmbeddingError("Compressed iTXt is not supported for genblaze manifest")
    cursor += 2  # skip compression flag + method
    lang_end = payload.find(b"\x00", cursor)
    if lang_end == -1:
        raise EmbeddingError("Malformed iTXt: missing language terminator")
    cursor = lang_end + 1
    tkw_end = payload.find(b"\x00", cursor)
    if tkw_end == -1:
        raise EmbeddingError("Malformed iTXt: missing translated keyword terminator")
    cursor = tkw_end + 1
    return payload[cursor:].decode("utf-8")


def _embed_chunks(data: bytes, manifest_json: str) -> bytes:
    """Return new PNG bytes with the genblaze iTXt chunk inserted after IHDR.

    Existing genblaze iTXt chunks are dropped, so re-embed produces exactly one.
    Every other chunk — including ancillary chunks (pHYs, gAMA, cHRM, sRGB,
    iCCP, tIME, bKGD, custom) — is copied verbatim.
    """
    new_itxt = _build_itxt(ITXT_KEY, manifest_json)
    out = bytearray(_PNG_SIGNATURE)
    saw_ihdr = False

    for pos, chunk_type, total in _walk_chunks(data):
        if chunk_type == b"iTXt":
            payload = data[pos + 8 : pos + 8 + (total - 12)]
            if _itxt_keyword(payload) == ITXT_KEY:
                continue  # drop existing genblaze chunk; re-add a single fresh one below
        out += data[pos : pos + total]
        if chunk_type == b"IHDR":
            saw_ihdr = True
            out += new_itxt  # iTXt right after IHDR is always spec-valid

    if not saw_ihdr:
        raise EmbeddingError("Not a valid PNG (no IHDR chunk)")
    return bytes(out)


def _extract_text(data: bytes) -> str:
    """Find the genblaze iTXt chunk and return its text payload."""
    for pos, chunk_type, total in _walk_chunks(data):
        if chunk_type != b"iTXt":
            continue
        payload_size = total - 12
        # Cap before slicing — a hostile PNG could declare a giant iTXt that
        # would otherwise allocate hundreds of MB only to be rejected later.
        if payload_size > MAX_MANIFEST_BYTES:
            raise EmbeddingError(
                f"Embedded manifest exceeds size limit "
                f"({payload_size} > {MAX_MANIFEST_BYTES} bytes)"
            )
        payload = data[pos + 8 : pos + 8 + payload_size]
        if _itxt_keyword(payload) == ITXT_KEY:
            return _itxt_text(payload)
    raise EmbeddingError("No genblaze manifest found")


class PngHandler(BaseMediaHandler):
    """Embed and extract manifests in PNG iTXt metadata chunks."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            data = read_media_bytes(source)
            new_data = _embed_chunks(data, manifest.to_canonical_json())
            with atomic_write(output) as tmp:
                tmp.write_bytes(new_data)
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in PNG: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            data = read_media_bytes(source)
            text = _extract_text(data)
            return Manifest.model_validate(json.loads(text))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from PNG: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["image/png"]
