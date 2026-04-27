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


def test_sniff_mime_routes_misnamed_file(tmp_path: Path) -> None:
    """A PNG-named file containing JPEG bytes should dispatch to JpegHandler."""
    from genblaze_core.media.embedder import guess_mime, sniff_mime

    misnamed = tmp_path / "looks_like.png"
    Image.new("RGB", (8, 8)).save(misnamed, "JPEG")  # JPEG bytes, .png suffix

    assert sniff_mime(misnamed) == "image/jpeg"
    assert guess_mime(misnamed) == "image/jpeg"  # content wins over extension


def test_sniff_mime_falls_back_to_extension(tmp_path: Path) -> None:
    """Unknown content but known extension still resolves via extension."""
    from genblaze_core.media.embedder import guess_mime, sniff_mime

    src = tmp_path / "raw.mp4"
    src.write_bytes(b"\x00\x00\x00\x10not really an mp4")
    assert sniff_mime(src) is None
    assert guess_mime(src) == "video/mp4"


def test_sniff_mime_signatures(tmp_path: Path) -> None:
    """Each supported magic-byte signature is recognized."""
    from genblaze_core.media.embedder import sniff_mime

    cases = {
        "png": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        "jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 12, "image/jpeg"),
        "webp": (b"RIFF\x00\x00\x00\x10WEBP" + b"\x00" * 4, "image/webp"),
        "wav": (b"RIFF\x00\x00\x00\x10WAVE" + b"\x00" * 4, "audio/wav"),
        "flac": (b"fLaC" + b"\x00" * 12, "audio/flac"),
        "mp3_id3": (b"ID3\x03" + b"\x00" * 12, "audio/mpeg"),
        "mp3_raw": (b"\xff\xfb" + b"\x00" * 14, "audio/mpeg"),
    }
    for name, (head, expected) in cases.items():
        f = tmp_path / name
        f.write_bytes(head)
        assert sniff_mime(f) == expected, f"sniff failed for {name}"


def test_embed_inline_success_has_no_error(tmp_path: Path) -> None:
    """A successful inline embed must report embed_error=None."""
    png = tmp_path / "test.png"
    Image.new("RGBA", (1, 1)).save(png)
    embedder = SmartEmbedder()
    result = embedder.embed(png, _make_manifest())
    assert result.method == "inline"
    assert result.embed_error is None


def test_embed_fallback_surfaces_error(tmp_path: Path) -> None:
    """Sidecar fallback after a failed inline embed must populate embed_error."""
    # .mp4 extension routes to Mp4Handler, which will reject 'fake video data'
    # as not-a-valid-MP4 — exercising the inline failure path.
    src = tmp_path / "broken.mp4"
    src.write_bytes(b"fake video data")

    embedder = SmartEmbedder()
    result = embedder.embed(src, _make_manifest())
    assert result.method == "sidecar"
    assert result.sidecar_path is not None
    assert result.embed_error is not None
    assert "EmbeddingError" in result.embed_error or "Mp4" in result.embed_error


def test_embed_unknown_extension_no_error(tmp_path: Path) -> None:
    """An extension with no handler goes straight to sidecar — not a failure, no error."""
    src = tmp_path / "test.xyz"
    src.write_bytes(b"unknown format data")
    embedder = SmartEmbedder()
    result = embedder.embed(src, _make_manifest())
    assert result.method == "sidecar"
    assert result.embed_error is None  # unknown != failure


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
