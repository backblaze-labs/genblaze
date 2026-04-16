"""FLAC media handler — embed/extract manifests via Vorbis comments."""

from __future__ import annotations

import json
from pathlib import Path

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler, MediaCapability
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
            import os
            import shutil
            import tempfile

            # Work on a temp copy to ensure atomic writes
            fd, tmp = tempfile.mkstemp(dir=Path(output).parent, suffix=".tmp")
            os.close(fd)
            try:
                shutil.copy2(source, tmp)
                audio = FLAC(tmp)
                # Set manifest as Vorbis comment tag
                audio[VORBIS_TAG] = manifest.to_canonical_json()
                audio.save()
                os.replace(tmp, output)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
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
