"""Tests for FLAC media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.flac import FlacHandler
from genblaze_core.models import Manifest


@pytest.fixture()
def tmp_flac(tmp_path: Path) -> Path:
    """Create a minimal valid FLAC file.

    FLAC format: 'fLaC' marker + STREAMINFO metadata block (mandatory, 34 bytes).
    """
    import struct

    p = tmp_path / "test.flac"
    # FLAC stream marker
    marker = b"fLaC"
    # STREAMINFO metadata block header:
    # bit 7 = 1 (last block), bits 6-0 = 0 (STREAMINFO type) => 0x80
    # 24-bit size = 34 bytes
    block_header = struct.pack(">I", (0x80 << 24) | 34)
    # STREAMINFO: block sizes, frame sizes, sample rate, channels, bps, samples, md5
    # 16-bit min block size = 4096
    # 16-bit max block size = 4096
    # 24-bit min frame size = 0
    # 24-bit max frame size = 0
    # 20-bit sample rate = 44100, 3-bit channels-1 = 0, 5-bit bps-1 = 15, 36-bit total samples = 0
    streaminfo = struct.pack(">HH", 4096, 4096)  # min/max block size
    streaminfo += b"\x00\x00\x00"  # min frame size (24-bit)
    streaminfo += b"\x00\x00\x00"  # max frame size (24-bit)
    # Sample rate (20 bits) | channels-1 (3 bits) | bps-1 (5 bits) | total samples high (4 bits)
    # 44100 = 0xAC44, channels-1=0, bps-1=15 (16-bit), total_samples=0
    # 0xAC44 << 12 | 0 << 9 | 15 << 4 | 0 = 0xAC440F0
    rate_chan_bps = 0x0AC440F0
    streaminfo += struct.pack(">I", rate_chan_bps)
    streaminfo += struct.pack(">I", 0)  # total samples low 32 bits
    streaminfo += b"\x00" * 16  # MD5 signature

    p.write_bytes(marker + block_header + streaminfo)
    return p


def test_flac_embed_and_extract(tmp_flac: Path, sample_manifest: Manifest) -> None:
    handler = FlacHandler()
    handler.embed(tmp_flac, sample_manifest)

    extracted = handler.extract(tmp_flac)
    assert extracted.canonical_hash == sample_manifest.canonical_hash
    assert extracted.run.steps[0].prompt == "hello"


def test_flac_verify(tmp_flac: Path, sample_manifest: Manifest) -> None:
    handler = FlacHandler()
    handler.embed(tmp_flac, sample_manifest)
    assert handler.verify(tmp_flac)


def test_flac_extract_no_manifest(tmp_flac: Path) -> None:
    handler = FlacHandler()
    with pytest.raises(EmbeddingError, match="No genblaze manifest"):
        handler.extract(tmp_flac)


def test_flac_embed_to_different_output(
    tmp_path: Path, tmp_flac: Path, sample_manifest: Manifest
) -> None:
    out = tmp_path / "out.flac"
    handler = FlacHandler()
    result = handler.embed(tmp_flac, sample_manifest, output=out)
    assert result == out
    assert handler.verify(out)


def test_flac_embed_replaces_existing(tmp_flac: Path, sample_manifest: Manifest) -> None:
    """Embedding twice should replace, not duplicate."""
    handler = FlacHandler()
    handler.embed(tmp_flac, sample_manifest)
    handler.embed(tmp_flac, sample_manifest)
    assert handler.verify(tmp_flac)


def test_flac_capabilities() -> None:
    assert FlacHandler.capabilities() == ["audio/flac"]
