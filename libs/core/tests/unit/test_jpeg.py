"""Tests for JPEG media handler."""

import struct
from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.jpeg import JpegHandler, _build_xmp
from genblaze_core.models import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from PIL import Image


def test_jpeg_embed_and_extract(tmp_jpeg: Path, sample_manifest: Manifest) -> None:
    handler = JpegHandler()
    handler.embed(tmp_jpeg, sample_manifest)

    extracted = handler.extract(tmp_jpeg)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_jpeg_embed_and_extract_accept_str_path(tmp_jpeg: Path, sample_manifest: Manifest) -> None:
    handler = JpegHandler()
    embed_result = handler.embed(str(tmp_jpeg), sample_manifest)
    assert isinstance(embed_result, Path)

    extracted = handler.extract(str(tmp_jpeg))
    assert extracted.canonical_hash == sample_manifest.canonical_hash


def test_jpeg_verify(tmp_jpeg: Path, sample_manifest: Manifest) -> None:
    handler = JpegHandler()
    handler.embed(tmp_jpeg, sample_manifest)
    assert handler.verify(tmp_jpeg)


def test_jpeg_extract_no_manifest(tmp_jpeg: Path) -> None:
    handler = JpegHandler()
    with pytest.raises(EmbeddingError, match="No XMP data"):
        handler.extract(tmp_jpeg)


def test_jpeg_embed_to_different_output(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "src.jpg"
    out = tmp_path / "out.jpg"
    Image.new("RGB", (10, 10)).save(src, "JPEG")

    handler = JpegHandler()
    result = handler.embed(src, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_jpeg_capabilities() -> None:
    assert JpegHandler.capabilities() == ["image/jpeg"]


def test_jpeg_extract_rejects_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extract() must refuse a container over MAX_FILE_BYTES via read_media_bytes,
    not buffer it with a raw read() — the CLI content-sniffs handlers from magic
    bytes, so an oversized JPEG-magic file must be capped like PNG/WAV are.

    Patches the cap low so the test file stays a few bytes (no 500MB+ alloc).
    """
    from genblaze_core.media import base as media_base

    monkeypatch.setattr(media_base, "MAX_FILE_BYTES", 10)

    src = tmp_path / "oversized.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)  # JPEG magic, > patched cap

    with pytest.raises(EmbeddingError, match="too large"):
        JpegHandler().extract(src)


def test_jpeg_extract_walks_past_foreign_xmp(tmp_path: Path, sample_manifest: Manifest) -> None:
    """A JPEG carrying a leading non-genblaze XMP packet (e.g. Photoshop)
    must still surface the genblaze manifest from a later packet."""
    src = tmp_path / "two_xmp.jpg"
    Image.new("RGB", (16, 16)).save(src, "JPEG")
    handler = JpegHandler()
    handler.embed(src, sample_manifest)

    # Splice a fake "Photoshop" XMP packet ahead of the genblaze one.
    original = src.read_bytes()
    foreign = (
        b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="" xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"'
        b' photoshop:CaptureSource="ai-test"/>'
        b"</rdf:RDF></x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )
    # Insert the foreign packet as its own XMP APP1 segment ahead of ours.
    genblaze_pos = next(s for s, m, e in _segments(original) if _is_genblaze(original, s, m, e))
    payload = _XMP_HEADER + foreign
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    src.write_bytes(original[:genblaze_pos] + segment + original[genblaze_pos:])

    # Extraction finds the genblaze packet despite the foreign one being first.
    extracted = handler.extract(src)
    assert extracted.canonical_hash == sample_manifest.canonical_hash


def test_jpeg_embed_atomic_on_failure(
    tmp_path: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must leave the source file untouched."""
    src = tmp_path / "atomic.jpg"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(src, "JPEG", quality=80)
    original_bytes = src.read_bytes()

    # Write a partial temp file, then fail — simulates a disk error mid-write.
    real_write = Path.write_bytes

    def boom(self: Path, data: bytes) -> int:
        real_write(self, data[:10])
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "write_bytes", boom)

    handler = JpegHandler()
    with pytest.raises(EmbeddingError):
        handler.embed(src, sample_manifest)

    monkeypatch.setattr(Path, "write_bytes", real_write)
    assert src.read_bytes() == original_bytes, "source corrupted by failed embed"
    # No leftover temp files in the directory either.
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_jpeg_embed_preserves_pixels(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Embedding must not alter decoded pixels."""
    np = pytest.importorskip("numpy")

    src = tmp_path / "quality.jpg"
    img = Image.new("RGB", (64, 64), (128, 64, 200))
    img.save(src, "JPEG", quality=95)

    # Read pixels before embed
    before = np.array(Image.open(src))

    handler = JpegHandler()
    handler.embed(src, sample_manifest)

    # Read pixels after embed
    after = np.array(Image.open(src))
    assert np.array_equal(before, after), "JPEG embed should not alter pixel data"


# --- Byte-preservation (#249) -------------------------------------------------
# These tests parse JPEG segments independently of the handler so a bug in the
# production walker can't mask itself.

_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


def _segments(data: bytes) -> list[tuple[int, int, int]]:
    """Return ``(start, marker, end)`` for each header segment before SOS."""
    assert data[:2] == b"\xff\xd8"
    out = []
    pos = 2
    while data[pos + 1] != 0xDA:
        assert data[pos] == 0xFF
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        out.append((pos, data[pos + 1], pos + 2 + length))
        pos += 2 + length
    return out


def _is_genblaze(data: bytes, start: int, marker: int, end: int) -> bool:
    payload = data[start + 4 : end]
    return marker == 0xE1 and payload.startswith(_XMP_HEADER) and b"<mf:manifest>" in payload


def _strip_genblaze(data: bytes) -> bytes:
    """Remove every genblaze XMP APP1 segment, leaving all other bytes as-is."""
    out = bytearray()
    cursor = 0
    for start, marker, end in _segments(data):
        if _is_genblaze(data, start, marker, end):
            out += data[cursor:start]
            cursor = end
    return bytes(out + data[cursor:])


def _genblaze_count(data: bytes) -> int:
    return sum(_is_genblaze(data, *seg) for seg in _segments(data))


def _other_manifest(prompt: str) -> Manifest:
    return Manifest.from_run(Run(steps=[Step(provider="test", model="m", prompt=prompt)]))


def test_jpeg_embed_is_byte_preserving(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Stripping the embedded segment must yield the exact original bytes (#249)."""
    src = tmp_path / "photo.jpg"
    Image.effect_noise((64, 64), 40).convert("RGB").save(src, "JPEG", quality=70)
    original = src.read_bytes()

    JpegHandler().embed(src, sample_manifest)
    after = src.read_bytes()

    assert after != original
    assert _genblaze_count(after) == 1
    assert _strip_genblaze(after) == original
    assert JpegHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_jpeg_embed_keeps_jfif_first(tmp_path: Path, sample_manifest: Manifest) -> None:
    """JFIF requires APP0 immediately after SOI; the XMP segment goes after it."""
    src = tmp_path / "jfif.jpg"
    Image.new("RGB", (8, 8)).save(src, "JPEG")
    assert src.read_bytes()[2:4] == b"\xff\xe0"

    JpegHandler().embed(src, sample_manifest)
    segs = _segments(src.read_bytes())
    assert segs[0][1] == 0xE0
    assert _is_genblaze(src.read_bytes(), *segs[1])


def test_jpeg_embed_preserves_exif_icc_and_foreign_xmp(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """EXIF, ICC and a third-party XMP packet all survive verbatim."""
    src = tmp_path / "rich.jpg"
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make
    foreign_xmp = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta><?xpacket end="w"?>'
    )
    Image.new("RGB", (16, 16), (9, 8, 7)).save(
        src, "JPEG", exif=exif, icc_profile=b"\x00" * 128, xmp=foreign_xmp
    )
    original = src.read_bytes()

    JpegHandler().embed(src, sample_manifest)
    after = src.read_bytes()

    assert _strip_genblaze(after) == original
    assert foreign_xmp in after
    with Image.open(src) as img:
        assert img.getexif()[0x010F] == "TestCam"
        assert img.info["icc_profile"] == b"\x00" * 128
    assert JpegHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_jpeg_reembed_replaces_manifest(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "twice.jpg"
    Image.new("RGB", (16, 16)).save(src, "JPEG")
    original = src.read_bytes()
    second = _other_manifest("second")

    handler = JpegHandler()
    handler.embed(src, sample_manifest)
    handler.embed(src, second)
    after = src.read_bytes()

    assert _genblaze_count(after) == 1
    assert after.count(b"<mf:manifest>") == 1
    assert _strip_genblaze(after) == original
    assert handler.extract(src).canonical_hash == second.canonical_hash


def test_jpeg_legacy_pillow_embed_still_extracts_and_reembeds(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """Files written by the old Pillow re-encode path remain readable, and a
    new embed replaces their genblaze segment instead of adding a second."""
    src = tmp_path / "legacy.jpg"
    legacy_xmp = _build_xmp(sample_manifest.to_canonical_json())
    Image.new("RGB", (16, 16)).save(src, "JPEG", xmp=legacy_xmp)

    handler = JpegHandler()
    assert handler.extract(src).canonical_hash == sample_manifest.canonical_hash

    second = _other_manifest("second")
    handler.embed(src, second)
    assert src.read_bytes().count(b"<mf:manifest>") == 1
    assert handler.extract(src).canonical_hash == second.canonical_hash


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"not a jpeg at all", "Not a valid JPEG"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "Truncated"),  # length runs past EOF
        (b"\xff\xd8\xff\xe0\x00\x04ab\xff\xd9", "no image scan"),  # EOI before SOS
        (b"\xff\xd8\x00\x00", "expected marker"),
    ],
)
def test_jpeg_embed_rejects_malformed(
    tmp_path: Path, sample_manifest: Manifest, payload: bytes, match: str
) -> None:
    src = tmp_path / "bad.jpg"
    src.write_bytes(payload)
    with pytest.raises(EmbeddingError, match=match):
        JpegHandler().embed(src, sample_manifest)
    assert src.read_bytes() == payload


def test_jpeg_embed_rejects_oversized_manifest(
    tmp_jpeg: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from genblaze_core.media import jpeg

    monkeypatch.setattr(jpeg, "MAX_XMP_BYTES", 16)
    original = tmp_jpeg.read_bytes()
    with pytest.raises(EmbeddingError, match="too large"):
        JpegHandler().embed(tmp_jpeg, sample_manifest)
    assert tmp_jpeg.read_bytes() == original


def test_jpeg_progressive_embed_is_byte_preserving(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """Multi-scan (progressive) JPEGs interleave tables with scans after the
    first SOS; everything from SOS on must be copied untouched."""
    src = tmp_path / "progressive.jpg"
    Image.effect_noise((48, 48), 30).convert("RGB").save(src, "JPEG", progressive=True)
    original = src.read_bytes()

    JpegHandler().embed(src, sample_manifest)

    assert _strip_genblaze(src.read_bytes()) == original


@pytest.mark.parametrize(
    ("xmp", "match"),
    [
        (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>', "No genblaze manifest"),
        (b"<x:xmpmeta><mf:manifest>{}", "unterminated"),
        (b"<x:xmpmeta><mf:manifest>\xff\xfe</mf:manifest></x:xmpmeta>", "not UTF-8"),
    ],
)
def test_jpeg_extract_rejects_bad_xmp(tmp_path: Path, xmp: bytes, match: str) -> None:
    src = tmp_path / "badxmp.jpg"
    Image.new("RGB", (8, 8)).save(src, "JPEG", xmp=xmp)
    with pytest.raises(EmbeddingError, match=match):
        JpegHandler().extract(src)


def _own_segment(manifest: Manifest) -> bytes:
    """The exact APP1 segment the handler writes for ``manifest``."""
    payload = _XMP_HEADER + _build_xmp(manifest.to_canonical_json())
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _exif_with_description(text: str) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010E] = text  # ImageDescription
    return exif


def test_jpeg_extract_ignores_manifest_planted_outside_xmp(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """A ``<mf:manifest>`` string in EXIF (ahead of our segment) must not
    shadow the embedded manifest — extraction reads XMP segments only."""
    planted = _other_manifest("planted")
    src = tmp_path / "planted.jpg"
    exif = _exif_with_description(
        "<mf:manifest>" + planted.to_canonical_json().replace("<", "&lt;") + "</mf:manifest>"
    )
    Image.new("RGB", (16, 16)).save(src, "JPEG", exif=exif)

    JpegHandler().embed(src, sample_manifest)

    assert JpegHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_jpeg_reembed_keeps_merged_third_party_packet(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """A packet another tool merged our manifest into carries its own
    properties; re-embed must keep it and the fresh segment must win."""
    merged = _build_xmp(sample_manifest.to_canonical_json()).replace(
        b"<mf:manifest>", b"<dc:creator>Alice</dc:creator><mf:manifest>"
    )
    src = tmp_path / "merged.jpg"
    Image.new("RGB", (16, 16)).save(src, "JPEG", xmp=merged)

    second = _other_manifest("second")
    JpegHandler().embed(src, second)
    after = src.read_bytes()

    assert b"<dc:creator>Alice</dc:creator>" in after
    assert JpegHandler().extract(src).canonical_hash == second.canonical_hash


def test_jpeg_embed_preserves_fill_bytes_and_trailing_data(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """0xFF fill bytes before a marker and bytes after EOI survive verbatim."""
    base = tmp_path / "base.jpg"
    Image.new("RGB", (8, 8)).save(base, "JPEG")
    data = base.read_bytes()
    original = data[:2] + b"\xff\xff" + data[2:] + b"TRAILER"  # fill before APP0
    src = tmp_path / "fill.jpg"
    src.write_bytes(original)

    JpegHandler().embed(src, sample_manifest)
    after = src.read_bytes()

    assert after.replace(_own_segment(sample_manifest), b"", 1) == original
    assert JpegHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_jpeg_embed_without_app0_goes_right_after_soi(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    base = tmp_path / "base.jpg"
    Image.new("RGB", (8, 8)).save(base, "JPEG")
    data = base.read_bytes()
    app0_end = 4 + struct.unpack(">H", data[4:6])[0]
    original = data[:2] + data[app0_end:]  # drop the JFIF APP0
    src = tmp_path / "noapp0.jpg"
    src.write_bytes(original)

    JpegHandler().embed(src, sample_manifest)

    assert src.read_bytes() == original[:2] + _own_segment(sample_manifest) + original[2:]
