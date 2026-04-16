"""PNG media handler — embed/extract manifests via iTXt chunks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.base import BaseMediaHandler
from genblaze_core.models.manifest import Manifest

ITXT_KEY = "genblaze:manifest"


class PngHandler(BaseMediaHandler):
    """Embed and extract manifests in PNG iTXt metadata chunks."""

    def embed(self, source: Path, manifest: Manifest, output: Path | None = None) -> Path:
        output = output or source
        try:
            with Image.open(source) as img:
                png_info = PngInfo()
                # Preserve existing text chunks (skip our key to avoid duplicates)
                for key, value in img.text.items():  # type: ignore[attr-defined]
                    if key != ITXT_KEY:
                        png_info.add_itxt(key, value)
                png_info.add_itxt(ITXT_KEY, manifest.to_canonical_json())
                # Preserve ICC profile if present
                icc_profile = img.info.get("icc_profile")
                save_kwargs: dict = {"pnginfo": png_info}
                if icc_profile:
                    save_kwargs["icc_profile"] = icc_profile
                # Atomic write: temp file + rename to prevent corruption
                fd, tmp = tempfile.mkstemp(dir=Path(output).parent, suffix=".tmp")
                os.close(fd)
                try:
                    img.save(tmp, format="PNG", **save_kwargs)
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
            raise EmbeddingError(f"Failed to embed manifest in PNG: {exc}") from exc

    def extract(self, source: Path) -> Manifest:
        try:
            with Image.open(source) as img:
                raw = img.text.get(ITXT_KEY)  # type: ignore[attr-defined]
            if raw is None:
                raise EmbeddingError(f"No genblaze manifest found in {source}")
            data = json.loads(raw)
            return Manifest.model_validate(data)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Failed to extract manifest from PNG: {exc}") from exc

    @staticmethod
    def capabilities() -> list[str]:
        return ["image/png"]
