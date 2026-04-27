"""AAC/M4A media handler — embed/extract manifests via MP4 freeform atoms."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability, atomic_write
from genblaze_core.models.manifest import Manifest

# Freeform atom key for genblaze manifest
FREEFORM_KEY = "----:genblaze:manifest"


class AacHandler(BaseMediaHandler):
    """Embed and extract manifests in AAC/M4A files via MP4 freeform atoms."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            from mutagen.mp4 import MP4, MP4FreeForm
        except ImportError as exc:
            raise EmbeddingError(
                "mutagen package required for AAC/M4A embedding. "
                "Install with: pip install genblaze-core[audio]"
            ) from exc

        try:
            with atomic_write(output) as tmp:
                shutil.copy2(source, tmp)
                audio = MP4(tmp)
                if audio.tags is None:
                    audio.add_tags()
                tags = audio.tags
                assert tags is not None  # guaranteed after add_tags()
                tags[FREEFORM_KEY] = [MP4FreeForm(manifest.to_canonical_json().encode("utf-8"))]
                audio.save()
            return output
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to embed manifest in AAC/M4A: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            from mutagen.mp4 import MP4
        except ImportError as exc:
            raise EmbeddingError(
                "mutagen package required for AAC/M4A extraction. "
                "Install with: pip install genblaze-core[audio]"
            ) from exc

        try:
            audio = MP4(source)
            values = audio.tags.get(FREEFORM_KEY) if audio.tags else None
            if not values:
                raise EmbeddingError(f"No genblaze manifest found in {source}")
            payload = bytes(values[0])
            # Cap before decode + parse — hostile freeform atoms could
            # otherwise OOM the consumer on extract.
            if len(payload) > MAX_MANIFEST_BYTES:
                raise EmbeddingError(
                    f"Embedded manifest exceeds size limit "
                    f"({len(payload)} > {MAX_MANIFEST_BYTES} bytes)"
                )
            manifest_json = payload.decode("utf-8")
            return Manifest.model_validate(json.loads(manifest_json))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from AAC/M4A: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["audio/aac", "audio/mp4", "audio/x-m4a"]

    @staticmethod
    def media_capabilities() -> list[MediaCapability]:
        return [
            MediaCapability(
                mime_type=mime,
                max_payload_bytes=None,
                metadata_location="MP4 freeform atom",
                strip_risk="medium",
            )
            for mime in ("audio/aac", "audio/mp4", "audio/x-m4a")
        ]
