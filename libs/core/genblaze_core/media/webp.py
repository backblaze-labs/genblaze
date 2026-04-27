"""WebP media handler — embed/extract manifests via XMP metadata."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from PIL import Image

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability, atomic_write
from genblaze_core.media.jpeg import MAX_XMP_BYTES, _build_xmp, _scan_xmp_for_manifest
from genblaze_core.models.manifest import Manifest

# Cap on the bytes scanned to detect the WebP codec chunk. WebP files using
# VP8X may contain leading ICCP/ALPH/EXIF/XMP chunks before the codec chunk;
# 64 KiB is more than enough to reach VP8/VP8L past any reasonable preface
# while keeping the read bounded for hostile input.
_WEBP_DETECT_BYTES = 64 * 1024


def _detect_lossless_webp(source: Path) -> bool:
    """Detect VP8L (lossless) WebP by walking RIFF chunks.

    Pillow does not consistently surface a 'lossless' flag for WebP sources,
    and a magic-byte sniff at a fixed offset misses VP8X containers where the
    codec chunk follows leading metadata (ICCP, ALPH, EXIF, XMP, ANIM).
    Returns False on read errors or if the codec chunk is past the scan cap.
    """
    try:
        with open(source, "rb") as f:
            data = f.read(_WEBP_DETECT_BYTES)
    except OSError:
        return False
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos : pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if fourcc == b"VP8L":
            return True
        if fourcc == b"VP8 " or fourcc == b"VP8\x20":
            return False  # Lossy codec chunk reached without seeing VP8L.
        # Skip this chunk's payload (RIFF chunks are word-aligned).
        pos += 8 + chunk_size + (chunk_size & 1)
    return False


class WebpHandler(BaseMediaHandler):
    """Embed and extract manifests in WebP XMP metadata."""

    def embed(
        self,
        source: Path,
        manifest: Manifest,
        output: Path | None = None,
        *,
        lossless: bool | None = None,
        quality: int = 90,
    ) -> Path:
        """Embed manifest into a WebP file.

        ``lossless=None`` (default) preserves the source codec — VP8L sources
        stay lossless, VP8 sources stay lossy. Explicitly pass ``True`` or
        ``False`` to force a specific encoding.
        """
        output = output or source
        try:
            manifest_json = manifest.to_canonical_json()
            xmp_data = _build_xmp(manifest_json)
            if len(xmp_data) > MAX_XMP_BYTES:
                raise EmbeddingError(
                    f"Manifest too large for WebP XMP ({len(xmp_data)} bytes > {MAX_XMP_BYTES}). "
                    "Use sidecar fallback."
                )
            effective_lossless = _detect_lossless_webp(source) if lossless is None else lossless
            with Image.open(source) as img, atomic_write(output) as tmp:
                img.save(tmp, "WEBP", xmp=xmp_data, lossless=effective_lossless, quality=quality)
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in WebP: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            with open(source, "rb") as f:
                data = f.read()
            manifest_json = _scan_xmp_for_manifest(data, source)
            return Manifest.model_validate(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from WebP: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["image/webp"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="image/webp",
                max_payload_bytes=MAX_XMP_BYTES,
                metadata_location="XMP",
                strip_risk="medium",
            )
        ]
