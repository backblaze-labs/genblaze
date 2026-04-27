"""FLAC media handler — embed/extract manifests via Vorbis comments."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability, atomic_write
from genblaze_core.models.manifest import Manifest

# Vorbis comment tag for genblaze manifest
VORBIS_TAG = "GENBLAZE_MANIFEST"


class FlacHandler(BaseMediaHandler):
    """Embed and extract manifests in FLAC files via Vorbis comments."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            from mutagen.flac import FLAC
        except ImportError as exc:
            raise EmbeddingError(
                "mutagen package required for FLAC embedding. "
                "Install with: pip install genblaze-core[audio]"
            ) from exc

        try:
            with atomic_write(output) as tmp:
                shutil.copy2(source, tmp)
                audio = FLAC(tmp)
                audio[VORBIS_TAG] = manifest.to_canonical_json()
                audio.save()
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in FLAC: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            from mutagen.flac import FLAC
        except ImportError as exc:
            raise EmbeddingError(
                "mutagen package required for FLAC extraction. "
                "Install with: pip install genblaze-core[audio]"
            ) from exc

        try:
            audio = FLAC(source)
            values = audio.get(VORBIS_TAG)
            if not values:
                raise EmbeddingError(f"No genblaze manifest found in {source}")
            manifest_json = values[0]
            # Cap before json.loads — a hostile FLAC with a large Vorbis
            # comment could otherwise OOM the consumer.
            if len(manifest_json.encode("utf-8")) > MAX_MANIFEST_BYTES:
                raise EmbeddingError(
                    f"Embedded manifest exceeds size limit "
                    f"({len(manifest_json)} > {MAX_MANIFEST_BYTES} bytes)"
                )
            return Manifest.model_validate(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from FLAC: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["audio/flac"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type="audio/flac",
                max_payload_bytes=None,
                metadata_location="Vorbis comment",
                strip_risk="medium",
            )
        ]
