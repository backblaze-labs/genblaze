"""Tests for MP4 media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.mp4 import Mp4Handler
from genblaze_core.models import Manifest


def test_mp4_embed_and_extract(tmp_mp4: Path, sample_manifest: Manifest) -> None:
    handler = Mp4Handler()
    handler.embed(tmp_mp4, sample_manifest)

    extracted = handler.extract(tmp_mp4)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_mp4_verify(tmp_mp4: Path, sample_manifest: Manifest) -> None:
    handler = Mp4Handler()
    handler.embed(tmp_mp4, sample_manifest)
    assert handler.verify(tmp_mp4)


def test_mp4_extract_no_manifest(tmp_mp4: Path) -> None:
    handler = Mp4Handler()
    with pytest.raises(EmbeddingError, match="No genblaze manifest"):
        handler.extract(tmp_mp4)


def test_mp4_embed_to_different_output(
    tmp_path: Path, tmp_mp4: Path, sample_manifest: Manifest
) -> None:
    out = tmp_path / "out.mp4"
    handler = Mp4Handler()
    result = handler.embed(tmp_mp4, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_mp4_embed_replaces_existing(tmp_mp4: Path, sample_manifest: Manifest) -> None:
    """Embedding twice should replace, not duplicate."""
    handler = Mp4Handler()
    handler.embed(tmp_mp4, sample_manifest)
    handler.embed(tmp_mp4, sample_manifest)
    assert handler.verify(tmp_mp4)


def test_mp4_invalid_file(tmp_path: Path, sample_manifest: Manifest) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not an mp4")
    handler = Mp4Handler()
    with pytest.raises(EmbeddingError, match="Not a valid MP4"):
        handler.embed(bad, sample_manifest)


def test_mp4_capabilities() -> None:
    assert Mp4Handler.capabilities() == ["video/mp4"]
