"""MP3 media handler — embed/extract manifests via ID3v2 TXXX frames."""

from __future__ import annotations

import json
from pathlib import Path

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability
from genblaze_core.models.manifest import Manifest

TXXX_DESC = "genblaze:manifest"


class Mp3Handler(BaseMediaHandler):
    """Embed and extract manifests in MP3 ID3v2 TXXX frames."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
        except ImportError as exc:
            raise EmbeddingError(
                "mutagen package required for MP3 embedding. "
                "Install with: pip install genblaze-core[audio]"
            ) from exc

        try:
            if output != source:
                import shutil

                shutil.copy2(source, output)

            try:
                tags = ID3(output)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delall(f"TXXX:{TXXX_DESC}")
            tags.add(TXXX(encoding=3, desc=TXXX_DESC, text=[manifest.to_canonical_json()]))
            tags.save(output)
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in MP3: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            from mutagen.id3 import ID3
        except ImportError as exc:
            raise EmbeddingError(
                "mutagen package required for MP3 extraction. "
                "Install with: pip install genblaze-core[audio]"
            ) from exc

        try:
            tags = ID3(source)
            frame = tags.get(f"TXXX:{TXXX_DESC}")
            if frame is None:
                raise EmbeddingError(f"No genblaze manifest found in {source}")
            manifest_json = frame.text[0]
            return Manifest.model_validate(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from MP3: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["audio/mpeg"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="audio/mpeg",
                max_payload_bytes=None,
                metadata_location="ID3v2 TXXX",
                strip_risk="medium",
            )
        ]
