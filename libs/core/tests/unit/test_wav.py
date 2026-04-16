"""Tests for WAV media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.wav import WavHandler
from genblaze_core.models import Manifest


def test_wav_embed_and_extract(tmp_wav: Path, sample_manifest: Manifest) -> None:
    handler = WavHandler()
    handler.embed(tmp_wav, sample_manifest)

    extracted = handler.extract(tmp_wav)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_wav_verify(tmp_wav: Path, sample_manifest: Manifest) -> None:
    handler = WavHandler()
    handler.embed(tmp_wav, sample_manifest)
    assert handler.verify(tmp_wav)


def test_wav_extract_no_manifest(tmp_wav: Path) -> None:
    handler = WavHandler()
    with pytest.raises(EmbeddingError, match="No genblaze manifest"):
        handler.extract(tmp_wav)


def test_wav_embed_to_different_output(
    tmp_path: Path, tmp_wav: Path, sample_manifest: Manifest
) -> None:
    out = tmp_path / "out.wav"
    handler = WavHandler()
    result = handler.embed(tmp_wav, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_wav_embed_replaces_existing(tmp_wav: Path, sample_manifest: Manifest) -> None:
    """Embedding twice should replace, not duplicate."""
    handler = WavHandler()
    handler.embed(tmp_wav, sample_manifest)
    handler.embed(tmp_wav, sample_manifest)
    assert handler.verify(tmp_wav)


def test_wav_invalid_file(tmp_path: Path, sample_manifest: Manifest) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    handler = WavHandler()
    with pytest.raises(EmbeddingError, match="Not a valid WAV"):
        handler.embed(bad, sample_manifest)


def test_wav_capabilities() -> None:
    assert WavHandler.capabilities() == ["audio/wav"]
