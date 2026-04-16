"""Tests for MP3 media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.mp3 import Mp3Handler
from genblaze_core.models import Manifest


def test_mp3_embed_and_extract(tmp_mp3: Path, sample_manifest: Manifest) -> None:
    handler = Mp3Handler()
    handler.embed(tmp_mp3, sample_manifest)

    extracted = handler.extract(tmp_mp3)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_mp3_verify(tmp_mp3: Path, sample_manifest: Manifest) -> None:
    handler = Mp3Handler()
    handler.embed(tmp_mp3, sample_manifest)
    assert handler.verify(tmp_mp3)


def test_mp3_extract_no_manifest(tmp_mp3: Path) -> None:
    handler = Mp3Handler()
    with pytest.raises(EmbeddingError, match="No genblaze manifest"):
        handler.extract(tmp_mp3)


def test_mp3_embed_to_different_output(
    tmp_path: Path, tmp_mp3: Path, sample_manifest: Manifest
) -> None:
    out = tmp_path / "out.mp3"
    handler = Mp3Handler()
    result = handler.embed(tmp_mp3, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_mp3_embed_replaces_existing(tmp_mp3: Path, sample_manifest: Manifest) -> None:
    """Embedding twice should replace, not duplicate."""
    handler = Mp3Handler()
    handler.embed(tmp_mp3, sample_manifest)
    handler.embed(tmp_mp3, sample_manifest)
    assert handler.verify(tmp_mp3)


def test_mp3_capabilities() -> None:
    assert Mp3Handler.capabilities() == ["audio/mpeg"]
