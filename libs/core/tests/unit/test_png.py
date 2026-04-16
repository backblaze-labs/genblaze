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
