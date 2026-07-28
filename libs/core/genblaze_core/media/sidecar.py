"""Sidecar media handler — store manifests as .json files alongside media."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError, ManifestError
from genblaze_core.media.base import BaseMediaHandler, atomic_write
from genblaze_core.models.manifest import Manifest, parse_manifest

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

    def _sidecar_path(self, source: str | os.PathLike[str]) -> Path:
        # Coerce so a str source (SmartEmbedder's sidecar fallback, or a
        # caller invoking SidecarHandler directly) doesn't hit with_suffix()
        # — a Path-only method — with the same confusing failure as #225.
        source = Path(source)
        return source.with_suffix(source.suffix + ".genblaze.json")

    def embed(
        self,
        source: str | os.PathLike[str],
        manifest: Manifest,
        output: str | os.PathLike[str] | None = None,
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
        try:
            # _sidecar_path() coerces source to Path — kept inside the try
            # so a malformed source (e.g. embedded NUL) raises EmbeddingError
            # like any other bad-source failure, not a bare ValueError.
            sidecar = self._sidecar_path(output or source)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            json_str = (
                manifest.to_embed_json(policy)
                if policy is not None
                else manifest.to_canonical_json()
            )
            with atomic_write(sidecar) as tmp:
                tmp.write_bytes(json_str.encode("utf-8"))
            return sidecar
        except (EmbeddingError, ManifestError):
            # ManifestError surfaces policy misuse (e.g. full-mode redaction);
            # propagate as-is so callers can recognize it distinct from I/O.
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to write sidecar: {exc}") from exc

    def extract(self, source: str | os.PathLike[str]) -> Manifest:
        """Extract manifest from a sidecar file.

        Raises PointerSidecarError if the sidecar contains a pointer-mode
        manifest (no embedded run data — only a URI to fetch).
        """
        try:
            # _sidecar_path() coerces source to Path — kept inside the try
            # for the same reason as embed() above.
            sidecar = self._sidecar_path(source)
            if not sidecar.exists():
                raise EmbeddingError(f"No sidecar file found at {sidecar}")
            # Cap sidecar size — attacker-controllable when the media file
            # ships paired with its sidecar (zip-bomb-shaped JSON OOMs the
            # consumer).
            size = sidecar.stat().st_size
            if size > MAX_MANIFEST_BYTES:
                raise EmbeddingError(
                    f"Sidecar exceeds size limit: {size} > {MAX_MANIFEST_BYTES} bytes"
                )
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            # Detect pointer-mode sidecar: has manifest_uri but no run data
            if "run" not in data and "manifest_uri" in data:
                raise PointerSidecarError(
                    manifest_uri=data["manifest_uri"],
                    canonical_hash=data.get("canonical_hash", ""),
                )
            return parse_manifest(data)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to read sidecar: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["*/*"]
