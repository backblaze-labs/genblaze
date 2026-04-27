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
    method: str  # "inline", "sidecar", "pointer", "none"
    embed_error: str | None = None  # set when inline embed failed and we fell back to sidecar


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

        # Pointer mode: SidecarHandler.embed already writes the pointer JSON
        # via the policy-aware path. Delegate rather than reimplement.
        if policy is not None and policy.embed_mode == "pointer":
            sidecar_path = SidecarHandler().embed(source, manifest, output, policy=policy)
            return EmbedResult(
                path=output or source,
                sidecar_path=sidecar_path,
                manifest_uri=manifest.manifest_uri,
                method="pointer",
            )

        # Validate policy. In non-pointer modes, to_embed_json() produces the
        # same bytes as to_canonical_json() — or raises ManifestError when
        # redaction would desynchronize hash and payload. We invoke it for
        # the validation side effect and pass the original manifest through
        # to avoid a wasteful serialize → parse → revalidate round-trip in
        # the embed hot path.
        if policy is not None:
            manifest.to_embed_json(policy)

        mime = mime_type or guess_mime(source)

        # Try format-specific handler, fall back to sidecar on any error.
        # Failure reason is preserved in EmbedResult.embed_error so callers
        # can distinguish silent fallback from explicit sidecar selection.
        inline_error: str | None = None
        try:
            handler = _get_handler_for_mime(mime)
            if handler is not None:
                result_path = handler.embed(source, manifest, output)
                return EmbedResult(
                    path=result_path,
                    sidecar_path=None,
                    manifest_uri=None,
                    method="inline",
                )
        except Exception as exc:
            inline_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Inline embed failed for %s (%s), falling back to sidecar: %s",
                source,
                mime,
                exc,
            )

        # Fallback to sidecar
        sidecar = SidecarHandler()
        sidecar_path = sidecar.embed(source, manifest, output)
        return EmbedResult(
            path=output or source,
            sidecar_path=sidecar_path,
            manifest_uri=None,
            method="sidecar",
            embed_error=inline_error,
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


def sniff_mime(path: Path) -> str | None:
    """Inspect file magic bytes to detect MIME type.

    Returns None if the file can't be read or the signature isn't recognized.
    Reads at most the first 16 bytes — bounded I/O even on hostile input.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if len(head) < 4:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"ID3") or head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3":
        return "audio/mpeg"
    if len(head) >= 12 and head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image/webp"
        if head[8:12] == b"WAVE":
            return "audio/wav"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        # ISO-BMFF container; brand resolves video vs audio. We default to
        # video/mp4 since that's the most common case for genblaze pipelines;
        # audio/mp4 (.m4a) callers should pass mime_type explicitly.
        return "video/mp4"
    return None


def guess_mime(path: Path) -> str:
    """Determine MIME type, preferring file content over extension.

    Magic-byte sniff wins when it identifies a known signature; falls back
    to extension lookup, then to ``application/octet-stream``. This makes a
    misnamed file (e.g. ``image.png`` containing JPEG bytes) dispatch to
    the correct handler instead of failing inside the wrong one.
    """
    return (
        sniff_mime(path)
        or _EXTENSION_MIME_MAP.get(path.suffix.lower())
        or "application/octet-stream"
    )
