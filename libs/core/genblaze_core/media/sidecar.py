"""Sidecar media handler — store manifests as .json files alongside media."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler
from genblaze_core.models.manifest import Manifest

if TYPE_CHECKING:
    from genblaze_core.models.policy import EmbedPolicy


class PointerSidecarError(EmbeddingError):
    """Raised when extract() encounters a pointer-mode sidecar.

    The manifest_uri attribute contains the URI to fetch the full manifest.
    """

    def __init__(self, manifest_uri: str, canonical_hash: str) -> None:
        self.manifest_uri = manifest_uri
        self.canonical_hash = canonical_hash
        super().__init__(
            f"Sidecar is a pointer (manifest_uri={manifest_uri}). "
            "Fetch the full manifest from the URI to extract."
        )


class SidecarHandler(BaseMediaHandler):
    """Store/retrieve manifests as JSON sidecar files."""

    def _sidecar_path(self, source: Path) -> Path:
        return source.with_suffix(source.suffix + ".genblaze.json")

    def embed(
        self,
        source: Path,
        manifest: Manifest,
        output: Path | None = None,
        *,
        policy: EmbedPolicy | None = None,
    ) -> Path:
        """Write manifest as a sidecar JSON file.

        Args:
            source: Path to the media file.
            manifest: The manifest to write.
            output: Optional override output path.
            policy: If set, apply embed policy (e.g. pointer mode, redaction).
        """
        sidecar = self._sidecar_path(output or source)
        try:
            # Choose JSON representation based on policy
            if policy is not None:
                json_str = manifest.to_embed_json(policy)
            else:
                json_str = manifest.to_canonical_json()

            # Atomic write: temp file + rename to prevent corruption
            fd, tmp = tempfile.mkstemp(dir=sidecar.parent, suffix=".tmp")
            fd_closed = False
            try:
                os.write(fd, json_str.encode("utf-8"))
                os.close(fd)
                fd_closed = True
                os.replace(tmp, sidecar)
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
            return sidecar
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to write sidecar: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        """Extract manifest from a sidecar file.

        Raises PointerSidecarError if the sidecar contains a pointer-mode
        manifest (no embedded run data — only a URI to fetch).
        """
        sidecar = self._sidecar_path(source)
        if not sidecar.exists():
            raise EmbeddingError(f"No sidecar file found at {sidecar}")
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            # Detect pointer-mode sidecar: has manifest_uri but no run data
            if "run" not in data and "manifest_uri" in data:
                raise PointerSidecarError(
                    manifest_uri=data["manifest_uri"],
                    canonical_hash=data.get("canonical_hash", ""),
                )
            return Manifest.model_validate(data)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to read sidecar: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["*/*"]
