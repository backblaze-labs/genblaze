"""SmartEmbedder — auto-fallback manifest embedder."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from genblaze_core.media.sidecar import SidecarHandler
from genblaze_core.models.manifest import Manifest

logger = logging.getLogger("genblaze.media")

if TYPE_CHECKING:
    from genblaze_core.models.policy import EmbedPolicy


@dataclass
class EmbedResult:
    """Result of an embed operation."""

    path: Path
    sidecar_path: Path | None
    manifest_uri: str | None
    method: str  # "inline", "sidecar"


def _get_handler_for_mime(mime_type: str):
    """Get the appropriate handler for a MIME type."""
    from genblaze_core.media import get_handler

    return get_handler(mime_type)


class SmartEmbedder:
    """Embeds manifests with automatic format detection and sidecar fallback.

    Tries format-specific handler first, falls back to sidecar on failure.
    Respects EmbedPolicy for redaction.
    """

    def embed(
        self,
        source: Path,
        manifest: Manifest,
        output: Path | None = None,
        *,
        policy: EmbedPolicy | None = None,
        mime_type: str | None = None,
    ) -> EmbedResult:
        """Embed manifest into media file with auto-fallback.

        Args:
            source: Path to the media file.
            manifest: Manifest to embed.
            output: Optional output path.
            policy: Optional embed policy for redaction.
            mime_type: MIME type override. Guessed from extension if not provided.
        """
        if policy is not None and policy.embed_mode == "none":
            return EmbedResult(
                path=output or source,
                sidecar_path=None,
                manifest_uri=None,
                method="none",
            )

        # Pointer mode: write pointer JSON to sidecar (not inline)
        if policy is not None and policy.embed_mode == "pointer":
            import os
            import tempfile

            pointer_json = manifest.to_embed_json(policy)
            sidecar = SidecarHandler()
            sidecar_path = sidecar._sidecar_path(output or source)
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write to match all other write paths
            fd, tmp = tempfile.mkstemp(dir=sidecar_path.parent, suffix=".tmp")
            fd_closed = False
            try:
                os.write(fd, pointer_json.encode("utf-8"))
                os.close(fd)
                fd_closed = True
                os.replace(tmp, sidecar_path)
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
            return EmbedResult(
                path=output or source,
                sidecar_path=sidecar_path,
                manifest_uri=manifest.manifest_uri,
                method="pointer",
            )

        # Apply policy redaction if needed
        if policy is not None:
            embed_json = manifest.to_embed_json(policy)
            import json

            redacted_manifest = Manifest.model_validate(json.loads(embed_json))
        else:
            redacted_manifest = manifest

        mime = mime_type or guess_mime(source)

        # Try format-specific handler, fall back to sidecar on any error
        try:
            handler = _get_handler_for_mime(mime)
            if handler is not None:
                result_path = handler.embed(source, redacted_manifest, output)
                return EmbedResult(
                    path=result_path,
                    sidecar_path=None,
                    manifest_uri=None,
                    method="inline",
                )
        except Exception as exc:
            logger.warning(
                "Inline embed failed for %s (%s), falling back to sidecar: %s",
                source,
                mime,
                exc,
            )

        # Fallback to sidecar
        sidecar = SidecarHandler()
        sidecar_path = sidecar.embed(source, redacted_manifest, output)
        return EmbedResult(
            path=output or source,
            sidecar_path=sidecar_path,
            manifest_uri=None,
            method="sidecar",
        )


_EXTENSION_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


def guess_mime(path: Path) -> str:
    """Guess MIME type from file extension."""
    return _EXTENSION_MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
