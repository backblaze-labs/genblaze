"""Tests for JPEG media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.jpeg import JpegHandler
from genblaze_core.models import Manifest
from PIL import Image


def test_jpeg_embed_and_extract(tmp_jpeg: Path, sample_manifest: Manifest) -> None:
    handler = JpegHandler()
    handler.embed(tmp_jpeg, sample_manifest)

    extracted = handler.extract(tmp_jpeg)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


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
    # Insert the foreign packet ahead of the existing genblaze packet.
    genblaze_pos = original.find(b"<x:xmpmeta")
    assert genblaze_pos != -1
    spliced = original[:genblaze_pos] + foreign + original[genblaze_pos:]
    src.write_bytes(spliced)

    # Walking scan finds the genblaze packet despite the foreign one being first.
    extracted = handler.extract(src)
    assert extracted.canonical_hash == sample_manifest.canonical_hash


def test_jpeg_embed_atomic_on_failure(
    tmp_path: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-save must leave the source file untouched."""
    src = tmp_path / "atomic.jpg"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(src, "JPEG", quality=80)
    original_bytes = src.read_bytes()

    # Force img.save to raise after the temp file would have been opened.
    real_save = Image.Image.save

    def boom(self, fp, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Image.Image, "save", boom)

    handler = JpegHandler()
    with pytest.raises(EmbeddingError):
        handler.embed(src, sample_manifest)

    monkeypatch.setattr(Image.Image, "save", real_save)
    assert src.read_bytes() == original_bytes, "source corrupted by failed embed"
    # No leftover temp files in the directory either.
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_jpeg_embed_preserves_pixels(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Embedding should not degrade image quality (quality='keep')."""
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
