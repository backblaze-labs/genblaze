"""WAV media handler — embed/extract manifests via LIST/INFO chunks."""

from __future__ import annotations

import json
import os
import struct
import tempfile
from pathlib import Path

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability, read_media_bytes
from genblaze_core.models.manifest import Manifest

# Custom INFO tag for genblaze manifest
INFO_TAG = b"IMFL"  # "Info GenBLaze" (tag kept for backward compat)


class WavHandler(BaseMediaHandler):
    """Embed and extract manifests in WAV LIST/INFO chunks."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            manifest_bytes = manifest.to_canonical_json().encode("utf-8")
            data = read_media_bytes(source)

            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise EmbeddingError(f"Not a valid WAV file: {source}")

            # Build INFO chunk: tag + null-terminated string (padded to even length)
            payload = manifest_bytes + b"\x00"
            if len(payload) % 2 != 0:
                payload += b"\x00"
            info_entry = INFO_TAG + struct.pack("<I", len(manifest_bytes) + 1) + payload

            # Build LIST/INFO chunk
            list_chunk = b"LIST" + struct.pack("<I", len(info_entry) + 4) + b"INFO" + info_entry

            cleaned = _remove_list_info(data)
            riff_data = cleaned[8:]
            new_data = (
                b"RIFF"
                + struct.pack("<I", len(riff_data) + len(list_chunk))
                + riff_data
                + list_chunk
            )

            # Atomic write: temp file + rename to prevent corruption
            fd, tmp = tempfile.mkstemp(dir=Path(output).parent, suffix=".tmp")
            fd_closed = False
            try:
                os.write(fd, new_data)
                os.close(fd)
                fd_closed = True
                os.replace(tmp, output)
            except BaseException:
                if not fd_closed:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in WAV: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            data = read_media_bytes(source)
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise EmbeddingError(f"Not a valid WAV file: {source}")

            manifest_json = _find_info_tag(data, INFO_TAG)
            if manifest_json is None:
                raise EmbeddingError(f"No genblaze manifest found in {source}")
            return Manifest.model_validate(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from WAV: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["audio/wav"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="audio/wav",
                max_payload_bytes=None,
                metadata_location="LIST/INFO chunk",
                strip_risk="low",
            )
        ]


def _find_info_tag(data: bytes, tag: bytes) -> str | None:
    """Search WAV chunks for a LIST/INFO block containing the given tag."""
    pos = 12  # Skip RIFF header
    while pos < len(data) - 8:
        chunk_id = data[pos : pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if pos + 8 + chunk_size > len(data):
            break  # Truncated/corrupt chunk
        chunk_data = data[pos + 8 : pos + 8 + chunk_size]

        if chunk_id == b"LIST" and chunk_data[:4] == b"INFO":
            info_pos = 4
            while info_pos < len(chunk_data) - 8:
                sub_id = chunk_data[info_pos : info_pos + 4]
                sub_size = struct.unpack("<I", chunk_data[info_pos + 4 : info_pos + 8])[0]
                if info_pos + 8 + sub_size > len(chunk_data):
                    break  # Truncated sub-chunk
                if sub_id == tag:
                    # Cap BEFORE slicing — don't pay the bytes-allocation
                    # cost for a sub-chunk we're about to reject. Slice
                    # only inside the matching branch so non-matching
                    # sub-chunks don't allocate either.
                    if sub_size > MAX_MANIFEST_BYTES:
                        raise EmbeddingError(
                            f"Embedded manifest exceeds size limit: "
                            f"{sub_size} > {MAX_MANIFEST_BYTES} bytes"
                        )
                    sub_data = chunk_data[info_pos + 8 : info_pos + 8 + sub_size]
                    return sub_data.rstrip(b"\x00").decode("utf-8")
                # Advance (sub-chunks are word-aligned)
                info_pos += 8 + sub_size
                if info_pos % 2 != 0:
                    info_pos += 1

        # Advance to next chunk (word-aligned)
        pos += 8 + chunk_size
        if pos % 2 != 0:
            pos += 1
    return None


def _remove_list_info(data: bytes) -> bytes:
    """Remove all LIST/INFO chunks from WAV data."""
    result = data[:12]  # RIFF header + WAVE
    pos = 12
    while pos < len(data) - 8:
        chunk_id = data[pos : pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        total = 8 + chunk_size
        if total % 2 != 0:
            total += 1

        # Guard against malformed chunk sizes exceeding remaining data
        if pos + total > len(data):
            result += data[pos:]
            break

        # Skip LIST/INFO chunks
        if chunk_id == b"LIST" and pos + 12 <= len(data) and data[pos + 8 : pos + 12] == b"INFO":
            pos += total
            continue

        result += data[pos : pos + total]
        pos += total

    # Update RIFF size
    result = result[:4] + struct.pack("<I", len(result) - 8) + result[8:]
    return result
