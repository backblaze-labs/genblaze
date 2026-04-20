"""JPEG media handler — embed/extract manifests via XMP metadata."""

from __future__ import annotations

import html
import json
from pathlib import Path

from PIL import Image

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability
from genblaze_core.models.manifest import Manifest

XMP_NS = "genblaze"
MAX_XMP_BYTES = 60 * 1024  # 60KB size guard


def _build_xmp(manifest_json: str) -> bytes:
    """Build an XMP packet containing the manifest JSON (XML-escaped)."""
    # XML-escape manifest JSON to prevent tag injection from prompt content
    escaped = html.escape(manifest_json, quote=False)
    xmp = (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        f' xmlns:mf="https://github.com/backblaze-labs/genblaze/ns/1.0/">'
        f'<rdf:Description rdf:about="">'
        f"<mf:manifest>{escaped}</mf:manifest>"
        f"</rdf:Description>"
        f"</rdf:RDF>"
        f"</x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    return xmp.encode("utf-8")


def _extract_from_xmp(xmp_bytes: bytes) -> str:
    """Extract manifest JSON from XMP bytes (XML-unescaped)."""
    xmp_str = xmp_bytes.decode("utf-8")
    start_tag = "<mf:manifest>"
    end_tag = "</mf:manifest>"
    start = xmp_str.find(start_tag)
    end = xmp_str.find(end_tag)
    if start == -1 or end == -1:
        raise EmbeddingError("No genblaze manifest found in XMP data")
    content_start = start + len(start_tag)
    if end <= content_start:
        raise EmbeddingError("Malformed XMP: manifest end tag before content start")
    raw = xmp_str[content_start:end]
    return html.unescape(raw)


class JpegHandler(BaseMediaHandler):
    """Embed and extract manifests in JPEG XMP metadata."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            manifest_json = manifest.to_canonical_json()
            xmp_data = _build_xmp(manifest_json)
            if len(xmp_data) > MAX_XMP_BYTES:
                raise EmbeddingError(
                    f"Manifest too large for JPEG XMP ({len(xmp_data)} bytes > {MAX_XMP_BYTES}). "
                    "Use sidecar fallback."
                )
            with Image.open(source) as img:
                exif = img.info.get("exif", b"")
                img.save(output, "JPEG", xmp=xmp_data, exif=exif, quality="keep")
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in JPEG: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            with open(source, "rb") as f:
                data = f.read()
            # Search for XMP packet in raw bytes
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
            raise EmbeddingError(f"Failed to extract manifest from JPEG: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["image/jpeg"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="image/jpeg",
                max_payload_bytes=MAX_XMP_BYTES,
                metadata_location="XMP",
                strip_risk="medium",
            )
        ]
