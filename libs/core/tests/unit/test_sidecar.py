"""Tests for sidecar media handler."""

import json
from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.sidecar import PointerSidecarError, SidecarHandler
from genblaze_core.models import Manifest
from genblaze_core.models.policy import EmbedPolicy


def test_embed_creates_sidecar(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    handler = SidecarHandler()
    result = handler.embed(src, sample_manifest)
    assert result == tmp_path / "image.png.genblaze.json"
    assert result.exists()


def test_extract_from_sidecar(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    handler = SidecarHandler()
    handler.embed(src, sample_manifest)
    extracted = handler.extract(src)
    assert extracted.canonical_hash == sample_manifest.canonical_hash


def test_verify_sidecar(tmp_path: Path, sample_manifest: Manifest) -> None:
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    handler = SidecarHandler()
    handler.embed(src, sample_manifest)
    assert handler.verify(src)


def test_extract_missing_sidecar(tmp_path: Path) -> None:
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    handler = SidecarHandler()
    with pytest.raises(EmbeddingError, match="No sidecar file"):
        handler.extract(src)


def test_embed_full_mode_private_prompt_raises(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Sidecar embed must propagate the ManifestError from to_embed_json
    when full-mode redaction would desync hash from payload.

    Users who want redaction must switch to embed_mode='pointer' — see
    test_pointer_sidecar_embed_and_extract.
    """
    import pytest
    from genblaze_core.exceptions import ManifestError
    from genblaze_core.models.enums import PromptVisibility

    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    policy = EmbedPolicy(prompt_visibility=PromptVisibility.PRIVATE)
    handler = SidecarHandler()
    with pytest.raises(ManifestError, match="embed_mode='pointer'"):
        handler.embed(src, sample_manifest, policy=policy)


def test_pointer_sidecar_embed_and_extract(tmp_path: Path, sample_manifest: Manifest) -> None:
    """Pointer-mode sidecar embeds URI-only JSON; extract raises PointerSidecarError."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    sample_manifest.manifest_uri = "https://storage.example.com/manifest.json"
    policy = EmbedPolicy(embed_mode="pointer")

    handler = SidecarHandler()
    sidecar = handler.embed(src, sample_manifest, policy=policy)

    # Sidecar should contain only pointer fields
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "run" not in data
    assert data["manifest_uri"] == "https://storage.example.com/manifest.json"
    assert "canonical_hash" in data

    # Extract should raise PointerSidecarError
    with pytest.raises(PointerSidecarError) as exc_info:
        handler.extract(src)
    assert exc_info.value.manifest_uri == "https://storage.example.com/manifest.json"
    assert exc_info.value.canonical_hash == sample_manifest.canonical_hash
