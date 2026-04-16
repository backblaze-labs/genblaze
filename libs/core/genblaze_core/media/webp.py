"""WebP media handler — embed/extract manifests via XMP metadata."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability
from genblaze_core.media.jpeg import MAX_XMP_BYTES, _build_xmp, _extract_from_xmp
from genblaze_core.models.manifest import Manifest


class WebpHandler(BaseMediaHandler):
    """Embed and extract manifests in WebP XMP metadata."""

    def embed(
        self,
        source: Path,
        manifest: Manifest,
        output: Path | None = None,
        *,
        lossless: bool = False,
        quality: int = 90,
    ) -> Path:
        output = output or source
        try:
            manifest_json = manifest.to_canonical_json()
            xmp_data = _build_xmp(manifest_json)
            if len(xmp_data) > MAX_XMP_BYTES:
                raise EmbeddingError(
                    f"Manifest too large for WebP XMP ({len(xmp_data)} bytes > {MAX_XMP_BYTES}). "
                    "Use sidecar fallback."
                )
            with Image.open(source) as img:
                img.save(output, "WEBP", xmp=xmp_data, lossless=lossless, quality=quality)
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in WebP: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            with open(source, "rb") as f:
                data = f.read()
            xmp_start = data.find(b"<x:xmpmeta")
            xmp_end = data.find(b"<?xpacket end", xmp_start)
            if xmp_start == -1 or xmp_end == -1:
                raise EmbeddingError(f"No XMP data found in {source}")
            close_pos = data.find(b"?>", xmp_end)
            if close_pos == -1:
                raise EmbeddingError(f"Malformed XMP packet in {source}: missing closing '?>'")
            xmp_end = close_pos + 2
            xmp_bytes = data[xmp_start:xmp_end]
            manifest_json = _extract_from_xmp(xmp_bytes)
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
