"""Tests for MP4 media handler."""

from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.mp4 import GENBLAZE_UUID_BYTES, Mp4Handler
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


def _fast_start_mp4_bytes() -> bytes:
    """Synthetic fast-start MP4 layout: ftyp | moov | mdat.

    We don't need a real codec-valid stbl — only the property that the
    byte offset of mdat (and therefore any offset moov.stco would point
    into) must be preserved across embed. The test verifies that by
    asserting the pre-embed bytes survive as a strict prefix.
    """
    ftyp = b"\x00\x00\x00\x14" + b"ftyp" + b"isom" + b"\x00\x00\x00\x00" + b"isom"
    # 32-byte moov filler standing in for mvhd + trak; real stco entries
    # would reference absolute offsets into mdat below.
    moov = b"\x00\x00\x00\x20" + b"moov" + (b"\x00" * 24)
    mdat_payload = b"SAMPLE_DATA_AT_KNOWN_POSITION"
    mdat = (len(mdat_payload) + 8).to_bytes(4, "big") + b"mdat" + mdat_payload
    return ftyp + moov + mdat


def test_mp4_embed_preserves_original_bytes_as_prefix(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """Embedding must never shift mdat or any pre-existing box.

    Fast-start MP4s (ftyp | moov | mdat) carry absolute sample offsets in
    moov.stco / co64 pointing into mdat. An insert-before-mdat strategy
    would shift mdat forward and invalidate every sample offset. We
    append at EOF instead — the original bytes remain byte-identical.
    """
    original = _fast_start_mp4_bytes()
    mp4 = tmp_path / "fast_start.mp4"
    mp4.write_bytes(original)

    Mp4Handler().embed(mp4, sample_manifest)
    embedded = mp4.read_bytes()

    assert embedded[: len(original)] == original, (
        "Embed shifted pre-existing bytes — moov stco offsets would now be stale"
    )

    # The tail must be a single, well-formed UUID box carrying our marker.
    tail = embedded[len(original) :]
    tail_box_size = int.from_bytes(tail[:4], "big")
    assert tail_box_size == len(tail), "Tail must be exactly one UUID box"
    assert tail[4:8] == b"uuid"
    assert tail[8:24] == GENBLAZE_UUID_BYTES


def test_mp4_extract_streaming_rejects_malformed_uuid_box(tmp_path: Path) -> None:
    """A uuid box with box_size < header+16 must not trigger an unbounded read.

    Before the guard, payload_size = box_size - header_size - 16 went negative
    and f.read(-N) reads to EOF — an OOM vector for multi-GB inputs. The fix
    skips the malformed box instead of attempting to read its payload.
    """
    ftyp = b"\x00\x00\x00\x14" + b"ftyp" + b"isom" + b"\x00\x00\x00\x00" + b"isom"
    # Declared size 20 but the 16 bytes at offset 8 happen to match our UUID —
    # worst case for the unguarded code path.
    bad_uuid_box = (20).to_bytes(4, "big") + b"uuid" + GENBLAZE_UUID_BYTES
    trailing = b"\x00" * 1024
    mp4 = tmp_path / "malformed.mp4"
    mp4.write_bytes(ftyp + bad_uuid_box + trailing)

    result = Mp4Handler()._extract_streaming(mp4, mp4.stat().st_size)
    assert result is None, (
        "Malformed uuid box must not return payload via negative-size f.read()"
    )


def test_mp4_embed_replace_keeps_prefix_stable(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Re-embedding strips the prior UUID and appends the new one — the
    pre-mdat prefix stays byte-identical across both embeds."""
    original = _fast_start_mp4_bytes()
    mp4 = tmp_path / "fast_start.mp4"
    mp4.write_bytes(original)

    handler = Mp4Handler()
    handler.embed(mp4, sample_manifest)
    handler.embed(mp4, sample_manifest)

    embedded = mp4.read_bytes()
    assert embedded[: len(original)] == original

    tail = embedded[len(original) :]
    assert int.from_bytes(tail[:4], "big") == len(tail), (
        "Tail must be exactly one UUID box after a second embed"
    )
    assert tail[4:8] == b"uuid"
    assert tail[8:24] == GENBLAZE_UUID_BYTES
