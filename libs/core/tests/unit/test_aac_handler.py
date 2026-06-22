"""Tests for AAC/M4A media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.aac import AacHandler
from genblaze_core.models import Manifest


@pytest.fixture()
def tmp_m4a(tmp_path: Path) -> Path:
    """Create a minimal valid M4A file using mutagen."""
    from mutagen.mp4 import MP4

    p = tmp_path / "test.m4a"
    # Minimal MP4/M4A: ftyp + moov boxes (mutagen needs moov to parse)
    ftyp = b"\x00\x00\x00\x18" + b"ftyp" + b"M4A " + b"\x00\x00\x00\x00" + b"M4A " + b"mp42"
    # Minimal moov box with mvhd sub-box. The timescale field (payload bytes
    # 12-15) MUST be non-zero: mutagen derives track length as duration/timescale
    # and mutagen 1.48.0 raises MP4StreamInfoError ("division by zero") on a zero
    # timescale, whereas 1.47 tolerated it. Keep payload at 100 bytes (box 0x6c).
    mvhd_payload = (
        b"\x00\x00\x00\x00"  # version + flags
        + b"\x00\x00\x00\x00"  # creation_time
        + b"\x00\x00\x00\x00"  # modification_time
        + b"\x00\x00\x03\xe8"  # timescale = 1000 (non-zero)
        + b"\x00\x00\x03\xe8"  # duration = 1000
        + b"\x00" * 80  # remaining mvhd fields
    )
    mvhd = b"\x00\x00\x00\x6c" + b"mvhd" + mvhd_payload
    moov = b"\x00\x00\x00\x74" + b"moov" + mvhd
    mdat = b"\x00\x00\x00\x08" + b"mdat"
    p.write_bytes(ftyp + moov + mdat)
    # Verify mutagen can open it
    MP4(p)
    return p


def test_aac_embed_and_extract(tmp_m4a: Path, sample_manifest: Manifest) -> None:
    handler = AacHandler()
    handler.embed(tmp_m4a, sample_manifest)

    extracted = handler.extract(tmp_m4a)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_aac_verify(tmp_m4a: Path, sample_manifest: Manifest) -> None:
    handler = AacHandler()
    handler.embed(tmp_m4a, sample_manifest)
    assert handler.verify(tmp_m4a)


def test_aac_extract_no_manifest(tmp_m4a: Path) -> None:
    handler = AacHandler()
    with pytest.raises(EmbeddingError, match="No genblaze manifest"):
        handler.extract(tmp_m4a)


def test_aac_embed_to_different_output(
    tmp_path: Path, tmp_m4a: Path, sample_manifest: Manifest
) -> None:
    out = tmp_path / "out.m4a"
    handler = AacHandler()
    result = handler.embed(tmp_m4a, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_aac_embed_replaces_existing(tmp_m4a: Path, sample_manifest: Manifest) -> None:
    """Embedding twice should replace, not duplicate."""
    handler = AacHandler()
    handler.embed(tmp_m4a, sample_manifest)
    handler.embed(tmp_m4a, sample_manifest)
    assert handler.verify(tmp_m4a)


def test_aac_capabilities() -> None:
    assert AacHandler.capabilities() == ["audio/aac", "audio/mp4", "audio/x-m4a"]
