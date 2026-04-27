"""Tests for PNG media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.png import PngHandler
from genblaze_core.models import Manifest
from PIL import Image


def test_embed_and_extract(tmp_png: Path, sample_manifest: Manifest) -> None:
    handler = PngHandler()
    handler.embed(tmp_png, sample_manifest)

    extracted = handler.extract(tmp_png)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_verify(tmp_png: Path, sample_manifest: Manifest) -> None:
    handler = PngHandler()
    handler.embed(tmp_png, sample_manifest)
    assert handler.verify(tmp_png)


def test_extract_no_manifest(tmp_png: Path) -> None:
    handler = PngHandler()
    with pytest.raises(EmbeddingError, match="No genblaze manifest"):
        handler.extract(tmp_png)


def test_embed_to_different_output(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "src.png"
    out = tmp_path / "out.png"
    Image.new("RGBA", (1, 1)).save(src)

    handler = PngHandler()
    result = handler.embed(src, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_capabilities() -> None:
    assert PngHandler.capabilities() == ["image/png"]


def _build_png_with_chunks(extra_chunks: list[tuple[bytes, bytes]]) -> bytes:
    """Construct a minimal valid PNG with caller-supplied chunks inserted between IHDR and IEND."""
    import struct
    import zlib

    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    # 1x1 RGB image, bit depth 8, color type 2 (RGB), default compression/filter/interlace.
    ihdr_payload = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    # Single IDAT for a 1x1 RGB white pixel: zlib-compress filter byte 0 + 3 bytes.
    raw = b"\x00\xff\xff\xff"
    idat_payload = zlib.compress(raw)

    out = sig + chunk(b"IHDR", ihdr_payload)
    for ctype, payload in extra_chunks:
        out += chunk(ctype, payload)
    out += chunk(b"IDAT", idat_payload)
    out += chunk(b"IEND", b"")
    return out


def test_png_preserves_phys_chunk(tmp_path: Path, sample_manifest: Manifest) -> None:
    """pHYs (pixel density) chunk must survive embed → extract round trip."""
    import struct

    src = tmp_path / "phys.png"
    phys_payload = struct.pack(">IIB", 2835, 2835, 1)  # 72 dpi × 72 dpi, units=meter
    src.write_bytes(_build_png_with_chunks([(b"pHYs", phys_payload)]))

    PngHandler().embed(src, sample_manifest)

    # Walk the embedded file's chunks and confirm pHYs is still there with identical bytes.
    after = src.read_bytes()
    assert b"pHYs" in after
    pos = after.find(b"pHYs")
    after_payload = after[pos + 4 : pos + 4 + 9]  # 4-byte type + 9-byte payload
    assert after_payload == phys_payload, "pHYs payload corrupted by embed"


def test_png_preserves_gama_and_chrm(tmp_path: Path, sample_manifest: Manifest) -> None:
    """gAMA and cHRM color-management chunks must survive embed."""
    import struct

    src = tmp_path / "color.png"
    gama_payload = struct.pack(">I", 45455)  # gamma 0.45455 × 100000
    chrm_payload = struct.pack(
        ">IIIIIIII",
        31270,
        32900,  # white point
        64000,
        33000,  # red
        30000,
        60000,  # green
        15000,
        6000,  # blue
    )
    src.write_bytes(_build_png_with_chunks([(b"gAMA", gama_payload), (b"cHRM", chrm_payload)]))

    PngHandler().embed(src, sample_manifest)
    after = src.read_bytes()

    gpos = after.find(b"gAMA")
    assert gpos != -1
    assert after[gpos + 4 : gpos + 4 + 4] == gama_payload

    cpos = after.find(b"cHRM")
    assert cpos != -1
    assert after[cpos + 4 : cpos + 4 + 32] == chrm_payload


def test_png_preserves_private_chunk(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Private/custom chunks (lowercase 4th char = ancillary, uppercase 1st = public)
    must round-trip — Pillow's saver would have dropped this entirely."""
    src = tmp_path / "private.png"
    private_payload = b"opaque tool-specific blob"
    src.write_bytes(_build_png_with_chunks([(b"prVt", private_payload)]))

    PngHandler().embed(src, sample_manifest)
    after = src.read_bytes()
    assert b"prVt" in after
    pos = after.find(b"prVt")
    assert after[pos + 4 : pos + 4 + len(private_payload)] == private_payload


def test_png_replaces_existing_genblaze_itxt(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Re-embedding must not duplicate the genblaze iTXt chunk."""
    src = tmp_path / "replace.png"
    Image.new("RGBA", (1, 1)).save(src)

    handler = PngHandler()
    handler.embed(src, sample_manifest)
    handler.embed(src, sample_manifest)
    handler.embed(src, sample_manifest)

    after = src.read_bytes()
    assert after.count(b"genblaze:manifest") == 1, "iTXt duplicated on re-embed"
    assert handler.verify(src)


def test_png_rejects_non_png_signature(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Bad signature must surface a clear error, not a confusing downstream failure."""
    bad = tmp_path / "fake.png"
    bad.write_bytes(b"not a png file")
    with pytest.raises(EmbeddingError, match="signature mismatch|valid PNG"):
        PngHandler().embed(bad, sample_manifest)
    with pytest.raises(EmbeddingError, match="signature mismatch|valid PNG"):
        PngHandler().extract(bad)


def test_png_rejects_oversized_itxt_payload(tmp_path: Path, sample_manifest: Manifest) -> None:
    """A hostile PNG with a multi-MB iTXt payload must be rejected before slicing."""
    import struct
    import zlib

    from genblaze_core._utils import MAX_MANIFEST_BYTES

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    # Construct an iTXt with our keyword and a payload that exceeds the cap.
    oversized_text = b"x" * (MAX_MANIFEST_BYTES + 1024)
    itxt_body = b"genblaze:manifest\x00\x00\x00\x00\x00" + oversized_text
    src = tmp_path / "oversized.png"
    src.write_bytes(
        sig + chunk(b"IHDR", ihdr_payload) + chunk(b"iTXt", itxt_body) + chunk(b"IEND", b"")
    )
    with pytest.raises(EmbeddingError, match="exceeds size limit"):
        PngHandler().extract(src)
