"""Tests for WebP media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.webp import WebpHandler
from genblaze_core.models import Manifest
from PIL import Image


def test_webp_embed_and_extract(tmp_webp: Path, sample_manifest: Manifest) -> None:
    handler = WebpHandler()
    handler.embed(tmp_webp, sample_manifest)

    extracted = handler.extract(tmp_webp)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


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


def test_webp_embed_preserves_pixels(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Embedding with lossless=True should preserve exact pixel data."""
    np = pytest.importorskip("numpy")

    src = tmp_path / "quality.webp"
    img = Image.new("RGB", (64, 64), (128, 64, 200))
    img.save(src, "WEBP", lossless=True)

    # Read pixels before embed
    before = np.array(Image.open(src))

    handler = WebpHandler()
    handler.embed(src, sample_manifest, lossless=True)

    # Read pixels after embed
    after = np.array(Image.open(src))
    assert np.array_equal(before, after), "WebP embed should not alter pixel data"


def test_webp_lossless_auto_detected(tmp_path: Path, sample_manifest: Manifest) -> None:
    """A lossless source must stay lossless even when caller doesn't pass lossless=True."""
    np = pytest.importorskip("numpy")

    src = tmp_path / "auto.webp"
    img = Image.new("RGB", (64, 64), (128, 64, 200))
    img.save(src, "WEBP", lossless=True)
    before = np.array(Image.open(src))

    handler = WebpHandler()
    handler.embed(src, sample_manifest)  # no lossless= argument

    after = np.array(Image.open(src))
    assert np.array_equal(before, after), (
        "VP8L source silently re-encoded as lossy when lossless was auto-detect"
    )


def test_webp_lossy_preserved_when_unspecified(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Lossy source stays lossy under auto-detect (no surprise lossless mode)."""
    src = tmp_path / "lossy.webp"
    Image.new("RGB", (32, 32)).save(src, "WEBP", quality=80)  # lossy
    handler = WebpHandler()
    handler.embed(src, sample_manifest)
    # File should still be a valid WebP and contain the manifest.
    assert handler.verify(src)


def test_detect_lossless_walks_past_leading_chunks(tmp_path: Path) -> None:
    """VP8X containers may carry ICCP/EXIF/XMP before the codec chunk.

    The detection must walk RIFF chunks rather than peeking at a fixed
    offset; otherwise lossless inputs from Lightroom / ImageMagick / similar
    tools get silently downgraded to lossy on embed.
    """
    import struct

    from genblaze_core.media.webp import _detect_lossless_webp

    iccp = b"\x00" * 512  # synthetic ICCP profile, large enough to push VP8L past byte 30
    vp8l = b"\x2f" + b"\x00" * 16  # VP8L signature byte + arbitrary tail
    vp8x = b"\x10\x00\x00\x00" + b"\xff\x00\x00" + b"\xff\x00\x00"  # 10-byte VP8X payload
    chunks = (
        b"VP8X"
        + struct.pack("<I", len(vp8x))
        + vp8x
        + b"ICCP"
        + struct.pack("<I", len(iccp))
        + iccp
        + b"VP8L"
        + struct.pack("<I", len(vp8l))
        + vp8l
    )
    body = b"WEBP" + chunks
    riff = b"RIFF" + struct.pack("<I", len(body)) + body

    src = tmp_path / "vp8x_with_iccp.webp"
    src.write_bytes(riff)
    assert _detect_lossless_webp(src) is True


def test_detect_lossy_vp8x_returns_false(tmp_path: Path) -> None:
    """VP8X container holding a VP8 (lossy) chunk must be detected as lossy."""
    import struct

    from genblaze_core.media.webp import _detect_lossless_webp

    vp8 = b"\x9d\x01\x2a" + b"\x00" * 16  # VP8 frame-tag prefix, padded
    vp8x = b"\x10\x00\x00\x00" + b"\xff\x00\x00" + b"\xff\x00\x00"
    chunks = (
        b"VP8X" + struct.pack("<I", len(vp8x)) + vp8x + b"VP8 " + struct.pack("<I", len(vp8)) + vp8
    )
    body = b"WEBP" + chunks
    riff = b"RIFF" + struct.pack("<I", len(body)) + body

    src = tmp_path / "vp8x_lossy.webp"
    src.write_bytes(riff)
    assert _detect_lossless_webp(src) is False


def test_webp_embed_atomic_on_failure(
    tmp_path: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-save must leave the source file untouched."""
    src = tmp_path / "atomic.webp"
    Image.new("RGB", (16, 16)).save(src, "WEBP", lossless=True)
    original_bytes = src.read_bytes()

    real_save = Image.Image.save

    def boom(self, fp, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Image.Image, "save", boom)
    handler = WebpHandler()
    with pytest.raises(EmbeddingError):
        handler.embed(src, sample_manifest)

    monkeypatch.setattr(Image.Image, "save", real_save)
    assert src.read_bytes() == original_bytes, "source corrupted by failed embed"
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"temp files leaked: {leftovers}"
