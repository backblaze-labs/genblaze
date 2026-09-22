"""Tests for WebP media handler."""

import struct
from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.jpeg import _build_xmp
from genblaze_core.media.webp import WebpHandler
from genblaze_core.models import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from PIL import Image


def test_webp_embed_and_extract(tmp_webp: Path, sample_manifest: Manifest) -> None:
    handler = WebpHandler()
    handler.embed(tmp_webp, sample_manifest)

    extracted = handler.extract(tmp_webp)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_webp_embed_and_extract_accept_str_path(tmp_webp: Path, sample_manifest: Manifest) -> None:
    handler = WebpHandler()
    embed_result = handler.embed(str(tmp_webp), sample_manifest)
    assert isinstance(embed_result, Path)

    extracted = handler.extract(str(tmp_webp))
    assert extracted.canonical_hash == sample_manifest.canonical_hash


def test_webp_verify(tmp_webp: Path, sample_manifest: Manifest) -> None:
    handler = WebpHandler()
    handler.embed(tmp_webp, sample_manifest)
    assert handler.verify(tmp_webp)


def test_webp_extract_no_manifest(tmp_webp: Path) -> None:
    handler = WebpHandler()
    with pytest.raises(EmbeddingError, match="No XMP data"):
        handler.extract(tmp_webp)


def test_webp_embed_to_different_output(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "src.webp"
    out = tmp_path / "out.webp"
    Image.new("RGB", (10, 10)).save(src, "WEBP")

    handler = WebpHandler()
    result = handler.embed(src, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_webp_capabilities() -> None:
    assert WebpHandler.capabilities() == ["image/webp"]


def test_webp_extract_rejects_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extract() must refuse a container over MAX_FILE_BYTES via read_media_bytes,
    not buffer it with a raw read() — the CLI content-sniffs handlers from magic
    bytes, so an oversized WebP-magic file must be capped like PNG/WAV are.

    Patches the cap low so the test file stays a few bytes (no 500MB+ alloc).
    """
    from genblaze_core.media import base as media_base

    monkeypatch.setattr(media_base, "MAX_FILE_BYTES", 10)

    src = tmp_path / "oversized.webp"
    src.write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64)  # WebP magic, > patched cap

    with pytest.raises(EmbeddingError, match="too large"):
        WebpHandler().extract(src)


def test_webp_lossless_embed_preserves_pixels(tmp_path: Path, sample_manifest: Manifest) -> None:
    """A lossless (VP8L) source keeps exact pixel data."""
    np = pytest.importorskip("numpy")

    src = tmp_path / "quality.webp"
    Image.new("RGB", (64, 64), (128, 64, 200)).save(src, "WEBP", lossless=True)
    before = np.array(Image.open(src))

    WebpHandler().embed(src, sample_manifest)

    after = np.array(Image.open(src))
    assert np.array_equal(before, after), "WebP embed should not alter pixel data"


def test_webp_embed_atomic_on_failure(
    tmp_path: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must leave the source file untouched."""
    src = tmp_path / "atomic.webp"
    Image.new("RGB", (16, 16)).save(src, "WEBP", lossless=True)
    original_bytes = src.read_bytes()

    # Write a partial temp file, then fail — simulates a disk error mid-write.
    real_write = Path.write_bytes

    def boom(self: Path, data: bytes) -> int:
        real_write(self, data[:10])
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "write_bytes", boom)
    handler = WebpHandler()
    with pytest.raises(EmbeddingError):
        handler.embed(src, sample_manifest)

    monkeypatch.setattr(Path, "write_bytes", real_write)
    assert src.read_bytes() == original_bytes, "source corrupted by failed embed"
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"temp files leaked: {leftovers}"


# --- Byte-preservation (#249) -------------------------------------------------
# RIFF parsing here is independent of the handler so a walker bug can't hide.

_XMP_FLAG = 0x04


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Return ``[(fourcc, verbatim_chunk_bytes), ...]`` for a WebP file."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 8, "RIFF size not updated"
    out = []
    pos = 12
    while pos < len(data):
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        end = pos + 8 + size + (size & 1)
        out.append((data[pos : pos + 4], data[pos:end]))
        pos = end
    return out


def _is_genblaze(fourcc: bytes, raw: bytes) -> bool:
    return fourcc == b"XMP " and b"<mf:manifest>" in raw


def _strip_genblaze(data: bytes, *, had_xmp: bool = False) -> bytes:
    """Undo an embed into an extended (VP8X) file: drop genblaze XMP chunks,
    restore the original XMP flag, and recompute the RIFF size."""
    body = bytearray(b"WEBP")
    for fourcc, raw in _chunks(data):
        if _is_genblaze(fourcc, raw):
            continue
        if fourcc == b"VP8X" and not had_xmp:
            raw = raw[:8] + bytes([raw[8] & ~_XMP_FLAG]) + raw[9:]
        body += raw
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body)


def _other_manifest(prompt: str) -> Manifest:
    return Manifest.from_run(Run(steps=[Step(provider="test", model="m", prompt=prompt)]))


@pytest.mark.parametrize(
    ("mode", "save_kwargs", "codec", "alpha"),
    [
        ("RGB", {"quality": 60}, b"VP8 ", False),
        ("RGB", {"lossless": True}, b"VP8L", False),
        ("RGBA", {"lossless": True}, b"VP8L", True),
    ],
)
def test_webp_simple_converts_to_extended_preserving_bitstream(
    tmp_path: Path,
    sample_manifest: Manifest,
    mode: str,
    save_kwargs: dict,
    codec: bytes,
    alpha: bool,
) -> None:
    """A simple-format file gains a VP8X header; the codec chunk is untouched."""
    src = tmp_path / "simple.webp"
    img = Image.effect_noise((37, 23), 40).convert(mode)
    if alpha:
        img.putalpha(Image.linear_gradient("L").resize((37, 23)))
    img.save(src, "WEBP", **save_kwargs)
    original = src.read_bytes()
    original_chunks = _chunks(original)
    assert [c[0] for c in original_chunks] == [codec], "fixture must be simple-format"
    with Image.open(src) as im:
        before = im.tobytes()

    WebpHandler().embed(src, sample_manifest)
    after = src.read_bytes()
    chunks = _chunks(after)

    assert [c[0] for c in chunks] == [b"VP8X", codec, b"XMP "]
    assert chunks[1][1] == original_chunks[0][1], "codec bitstream altered"
    vp8x = chunks[0][1][8:]
    assert vp8x[0] & _XMP_FLAG
    assert bool(vp8x[0] & 0x10) is alpha
    width = int.from_bytes(vp8x[4:7], "little") + 1
    height = int.from_bytes(vp8x[7:10], "little") + 1
    assert (width, height) == (37, 23)
    with Image.open(src) as im:
        assert im.tobytes() == before
    assert WebpHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_webp_extended_embed_is_byte_preserving(tmp_path: Path, sample_manifest: Manifest) -> None:
    """For a VP8X source, stripping the XMP chunk + flag yields the original bytes."""
    src = tmp_path / "extended.webp"
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    Image.new("RGB", (16, 16), (5, 6, 7)).save(
        src, "WEBP", exif=exif, icc_profile=b"\x00" * 64, quality=70
    )
    original = src.read_bytes()
    assert _chunks(original)[0][0] == b"VP8X"

    WebpHandler().embed(src, sample_manifest)
    after = src.read_bytes()

    assert _strip_genblaze(after) == original
    with Image.open(src) as im:
        assert im.getexif()[0x010F] == "TestCam"
    assert WebpHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_webp_animated_embed_is_byte_preserving(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "anim.webp"
    frames = [Image.new("RGB", (8, 8), (i * 40, 0, 0)) for i in range(3)]
    frames[0].save(src, "WEBP", save_all=True, append_images=frames[1:], duration=50)
    original = src.read_bytes()

    WebpHandler().embed(src, sample_manifest)

    assert _strip_genblaze(src.read_bytes()) == original
    with Image.open(src) as im:
        assert im.n_frames == 3


def test_webp_reembed_replaces_manifest(tmp_webp: Path, sample_manifest: Manifest) -> None:
    handler = WebpHandler()
    handler.embed(tmp_webp, sample_manifest)
    once = tmp_webp.read_bytes()
    second = _other_manifest("second")
    handler.embed(tmp_webp, second)
    after = tmp_webp.read_bytes()

    assert sum(_is_genblaze(*c) for c in _chunks(after)) == 1
    assert after.count(b"<mf:manifest>") == 1
    # Everything but the XMP chunk is unchanged between embeds.
    assert [c for c in _chunks(after) if c[0] != b"XMP "] == [
        c for c in _chunks(once) if c[0] != b"XMP "
    ]
    assert handler.extract(tmp_webp).canonical_hash == second.canonical_hash


def test_webp_preserves_foreign_xmp_chunk(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "foreign.webp"
    foreign_xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'
    Image.new("RGB", (8, 8)).save(src, "WEBP", xmp=foreign_xmp)
    original = src.read_bytes()

    WebpHandler().embed(src, sample_manifest)

    assert _strip_genblaze(src.read_bytes(), had_xmp=True) == original
    assert WebpHandler().extract(src).canonical_hash == sample_manifest.canonical_hash


def test_webp_legacy_pillow_embed_still_extracts_and_reembeds(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """Files written by the old Pillow re-encode path stay readable, and a
    new embed replaces their genblaze chunk instead of adding a second."""
    src = tmp_path / "legacy.webp"
    Image.new("RGB", (8, 8)).save(src, "WEBP", xmp=_build_xmp(sample_manifest.to_canonical_json()))

    handler = WebpHandler()
    assert handler.extract(src).canonical_hash == sample_manifest.canonical_hash

    second = _other_manifest("second")
    handler.embed(src, second)
    assert src.read_bytes().count(b"<mf:manifest>") == 1
    assert handler.extract(src).canonical_hash == second.canonical_hash


def test_webp_reencode_kwargs_are_deprecated_noops(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """``lossless=``/``quality=`` used to drive a re-encode; they now warn and
    must not change the bitstream."""
    src = tmp_path / "kw.webp"
    Image.new("RGB", (8, 8)).save(src, "WEBP", lossless=True)
    codec_before = _chunks(src.read_bytes())[0][1]

    with pytest.warns(DeprecationWarning, match="lossless"):
        WebpHandler().embed(src, sample_manifest, lossless=False, quality=10)

    assert _chunks(src.read_bytes())[1][1] == codec_before


def _riff(chunks: bytes) -> bytes:
    body = b"WEBP" + chunks
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"RIFX\x00\x00\x00\x00WEBP", "Not a valid WebP"),
        (b"RIFF\xff\x00\x00\x00WEBP", "Truncated"),  # RIFF size past EOF
        (_riff(b"VP8L" + struct.pack("<I", 50) + b"\x2f"), "Truncated"),  # chunk past RIFF end
        (_riff(b""), "no chunks"),
        (_riff(b"ICCP" + struct.pack("<I", 2) + b"ab"), "Unsupported WebP layout"),
        (_riff(b"VP8L" + struct.pack("<I", 2) + b"\x00\x00"), "Malformed VP8L"),
        (_riff(b"VP8 " + struct.pack("<I", 4) + b"\x00" * 4), "Malformed VP8"),
        (
            _riff(b"VP8 " + struct.pack("<I", 10) + b"\x00" * 3 + b"\x9d\x01\x2a" + b"\x00" * 4),
            "zero",
        ),
        (_riff(b"VP8X" + struct.pack("<I", 4) + b"\x00" * 4), "Malformed VP8X"),
    ],
)
def test_webp_embed_rejects_malformed(
    tmp_path: Path, sample_manifest: Manifest, payload: bytes, match: str
) -> None:
    src = tmp_path / "bad.webp"
    src.write_bytes(payload)
    with pytest.raises(EmbeddingError, match=match):
        WebpHandler().embed(src, sample_manifest)
    assert src.read_bytes() == payload


def test_webp_embed_rejects_oversized_manifest(
    tmp_webp: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from genblaze_core.media import webp

    monkeypatch.setattr(webp, "MAX_XMP_BYTES", 16)
    original = tmp_webp.read_bytes()
    with pytest.raises(EmbeddingError, match="too large"):
        WebpHandler().embed(tmp_webp, sample_manifest)
    assert tmp_webp.read_bytes() == original
