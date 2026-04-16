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
