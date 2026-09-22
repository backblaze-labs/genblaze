"""Sidecar media handler — store manifests as .json files alongside media."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError, ManifestError
from genblaze_core.media.base import MAX_MMAP_BYTES, BaseMediaHandler, atomic_write
from genblaze_core.models.manifest import Manifest, parse_manifest

if TYPE_CHECKING:
    from genblaze_core.models.policy import EmbedPolicy

# Sidecar/pointer mode's output= copy is a byte-for-byte duplication, not a
# transform, so — unlike the inline handlers, which need the full buffer in
# memory to rewrite it — it doesn't need read_media_bytes()'s in-memory
# buffer at all. Streaming keeps peak memory flat regardless of file size,
# and the cap matches MAX_MMAP_BYTES (Mp4Handler's own ceiling) rather than
# read_media_bytes()'s smaller MAX_FILE_BYTES, so this copy doesn't quietly
# break Mp4Handler's documented ">500MB, use sidecar fallback" escape hatch
# (#238).
_MAX_COPY_BYTES = MAX_MMAP_BYTES
_COPY_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB


def _copy_media(source: Path, target: Path) -> None:
    """Stream-copy source's bytes to target, atomically and without
    materializing the whole file in memory."""
    size = source.stat().st_size
    if size > _MAX_COPY_BYTES:
        raise EmbeddingError(
            f"File too large for sidecar copy ({size} bytes, limit {_MAX_COPY_BYTES})"
        )
    with atomic_write(target) as tmp, open(source, "rb") as src_f, open(tmp, "wb") as dst_f:
        shutil.copyfileobj(src_f, dst_f, length=_COPY_CHUNK_BYTES)


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

        Sidecar mode never rewrites media bytes in place, but the media file
        still must exist at the path callers get back. When ``output`` names
        a location distinct from ``source``, the source bytes are copied
        there first — mirroring how the inline handlers materialize
        ``output`` — so the sidecar always sits next to real media, not a
        pointer to nothing (#238). The copy and the sidecar write are each
        individually atomic (temp file + rename), but not as a single
        transaction: if the sidecar write fails after the copy succeeds, the
        copied media is left in place without a sidecar. Callers always get
        an exception in that case — never a false success — so this can't
        reproduce #238's silent-lie failure mode, just a leftover file.

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
            source = Path(source)
            target = Path(output) if output else source
            # Validate/serialize before touching the filesystem — a bad
            # policy (e.g. full-mode redaction) should fail without leaving
            # a half-copied media file behind.
            json_str = (
                manifest.to_embed_json(policy)
                if policy is not None
                else manifest.to_canonical_json()
            )
            sidecar = self._sidecar_path(target)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            # resolve() normalizes '..'/'.'  and relative-vs-absolute
            # spellings of the same file without requiring `target` to
            # exist yet, so a same-path output (the common case) is
            # correctly treated as a no-op rather than a needless copy.
            if target.resolve() != source.resolve():
                _copy_media(source, target)
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
