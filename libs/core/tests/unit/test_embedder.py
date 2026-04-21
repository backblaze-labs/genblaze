"""Tests for SmartEmbedder with auto-fallback."""

from pathlib import Path

from genblaze_core.media.embedder import SmartEmbedder
from genblaze_core.models import Manifest, Run, Step
from genblaze_core.models.enums import PromptVisibility
from genblaze_core.models.policy import EmbedPolicy
from PIL import Image


def _make_manifest() -> Manifest:
    step = Step(provider="test", model="test-model", prompt="hello", seed=42)
    run = Run(steps=[step])
    m = Manifest(run=run)
    m.compute_hash()
    return m


def test_embed_png_inline(tmp_path: Path) -> None:
    """SmartEmbedder should use inline embed for PNG."""
    png = tmp_path / "test.png"
    Image.new("RGBA", (1, 1)).save(png)

    embedder = SmartEmbedder()
    result = embedder.embed(png, _make_manifest())
    assert result.method == "inline"
    assert result.sidecar_path is None


def test_embed_unknown_format_sidecar(tmp_path: Path) -> None:
    """SmartEmbedder should fallback to sidecar for unknown formats."""
    src = tmp_path / "test.mp4"
    src.write_bytes(b"fake video data")

    embedder = SmartEmbedder()
    result = embedder.embed(src, _make_manifest())
    assert result.method == "sidecar"
    assert result.sidecar_path is not None
    assert result.sidecar_path.exists()


def test_embed_none_policy(tmp_path: Path) -> None:
    """embed_mode=none should skip embedding entirely."""
    png = tmp_path / "test.png"
    Image.new("RGBA", (1, 1)).save(png)

    embedder = SmartEmbedder()
    policy = EmbedPolicy(embed_mode="none")
    result = embedder.embed(png, _make_manifest(), policy=policy)
    assert result.method == "none"


def test_embed_jpeg_inline(tmp_path: Path) -> None:
    """SmartEmbedder should use inline embed for JPEG."""
    jpg = tmp_path / "test.jpg"
    Image.new("RGB", (10, 10)).save(jpg, "JPEG")

    embedder = SmartEmbedder()
    result = embedder.embed(jpg, _make_manifest())
    assert result.method == "inline"


def test_embed_truly_unknown_extension_sidecar(tmp_path: Path) -> None:
    """Truly unknown extension (no handler) should go directly to sidecar."""
    src = tmp_path / "test.xyz"
    src.write_bytes(b"unknown format data")

    embedder = SmartEmbedder()
    result = embedder.embed(src, _make_manifest())
    assert result.method == "sidecar"
    assert result.sidecar_path is not None
    assert result.sidecar_path.exists()


def test_embed_full_mode_private_prompt_raises(tmp_path: Path) -> None:
    """SmartEmbedder must surface ManifestError when the caller requests
    redaction without switching to pointer mode.

    Previously, the embedder would silently write a manifest whose
    canonical_hash did not match its embedded (redacted) payload. The
    correct UX is to push the caller to pointer mode, which preserves
    verifiability.
    """
    import pytest
    from genblaze_core.exceptions import ManifestError

    png = tmp_path / "test.png"
    Image.new("RGBA", (1, 1)).save(png)

    embedder = SmartEmbedder()
    policy = EmbedPolicy(prompt_visibility=PromptVisibility.PRIVATE)
    with pytest.raises(ManifestError, match="embed_mode='pointer'"):
        embedder.embed(png, _make_manifest(), policy=policy)


def test_embed_pointer_mode_preserves_verifiability(tmp_path: Path) -> None:
    """The escape hatch: redact via embed_mode='pointer' so the embedded
    pointer's hash still verifies against the server-held full manifest."""
    import json

    png = tmp_path / "test.png"
    Image.new("RGBA", (1, 1)).save(png)

    manifest = _make_manifest()
    manifest.manifest_uri = "https://example.com/manifests/abc.json"

    embedder = SmartEmbedder()
    policy = EmbedPolicy(embed_mode="pointer", prompt_visibility=PromptVisibility.PRIVATE)
    result = embedder.embed(png, manifest, policy=policy)
    assert result.method == "pointer"
    assert result.sidecar_path is not None

    pointer = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert pointer["canonical_hash"] == manifest.canonical_hash
    # Server-held full manifest still verifies.
    assert manifest.verify()


def test_embed_pointer_mode_writes_sidecar(tmp_path: Path) -> None:
    """embed_mode=pointer should write pointer JSON to sidecar, not leak full manifest."""
    import json

    png = tmp_path / "test.png"
    Image.new("RGBA", (1, 1)).save(png)

    manifest = _make_manifest()
    manifest.manifest_uri = "https://example.com/manifests/abc.json"

    embedder = SmartEmbedder()
    policy = EmbedPolicy(embed_mode="pointer")
    result = embedder.embed(png, manifest, policy=policy)

    assert result.method == "pointer"
    assert result.sidecar_path is not None
    assert result.sidecar_path.exists()
    assert result.manifest_uri == "https://example.com/manifests/abc.json"

    # Verify sidecar contains only pointer data (no run/steps)
    pointer_data = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert "canonical_hash" in pointer_data
    assert "manifest_uri" in pointer_data
    assert "run" not in pointer_data
