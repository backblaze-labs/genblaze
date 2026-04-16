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
