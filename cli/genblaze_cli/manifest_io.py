"""Shared CLI manifest loading helpers."""

from __future__ import annotations

import json
from pathlib import Path

from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media import get_handler, guess_mime
from genblaze_core.media.sidecar import PointerSidecarError, SidecarHandler
from genblaze_core.models.manifest import Manifest, parse_manifest


def load_standalone_json_manifest(file: Path) -> Manifest:
    size = file.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise EmbeddingError(
            f"Manifest JSON exceeds size limit: {size} > {MAX_MANIFEST_BYTES} bytes"
        )
    data = json.loads(file.read_text(encoding="utf-8"))
    if "run" not in data and "manifest_uri" in data:
        raise PointerSidecarError(
            manifest_uri=data["manifest_uri"],
            canonical_hash=data.get("canonical_hash", ""),
        )
    return parse_manifest(data)


def extract_manifest(file: Path) -> Manifest:
    """Extract a manifest from standalone JSON, media, or sidecar files."""
    if file.suffix.lower() == ".json":
        return load_standalone_json_manifest(file)

    mime = guess_mime(file)
    handler = get_handler(mime)
    if handler is not None:
        try:
            return handler.extract(file)
        except EmbeddingError:
            pass
    return SidecarHandler().extract(file)
