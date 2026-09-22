"""Tests for sidecar media handler."""

import json
from pathlib import Path

import pytest
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media.sidecar import PointerSidecarError, SidecarHandler
from genblaze_core.models import Manifest
from genblaze_core.models.enums import PromptVisibility, StepStatus
from genblaze_core.models.policy import EmbedPolicy
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step


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


def test_embed_and_extract_accept_str_path(tmp_path: Path, sample_manifest: Manifest) -> None:
    """_sidecar_path() calls Path.with_suffix() — same class of bug as #225's
    Mp4Handler, since SmartEmbedder's fallback and direct callers may pass str."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    handler = SidecarHandler()
    handler.embed(str(src), sample_manifest)
    extracted = handler.extract(str(src))
    assert extracted.canonical_hash == sample_manifest.canonical_hash


def test_embed_accepts_str_output_override(tmp_path: Path, sample_manifest: Manifest) -> None:
    """_sidecar_path(output or source) — the output= str branch, not just
    source=, must also be coerced (embed()'s `output or source` ternary)."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")
    out = tmp_path / "renamed.png"

    handler = SidecarHandler()
    result = handler.embed(src, sample_manifest, output=str(out))
    assert result == out.with_suffix(out.suffix + ".genblaze.json")
    assert result.exists()


def test_extract_from_sidecar_uses_parse_manifest_invariants(tmp_path: Path) -> None:
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")
    step = Step(
        provider="test",
        model="test-model",
        prompt="secret",
        prompt_visibility=PromptVisibility.ENCRYPTED,
        status=StepStatus.SUCCEEDED,
    )
    manifest = Manifest.from_run(Run(name="encrypted", steps=[step]))
    sidecar = src.with_suffix(src.suffix + ".genblaze.json")
    sidecar.write_text(manifest.to_canonical_json(), encoding="utf-8")

    with pytest.raises(EmbeddingError, match="encrypted"):
        SidecarHandler().extract(src)


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


def test_embed_full_mode_private_prompt_with_distinct_output_leaves_no_files(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """A rejected policy must fail before ANY filesystem write — including
    the #238 media copy — even when output= names a distinct, not-yet-created
    nested path. Guards the ordering in embed(): to_embed_json(policy) must
    run before mkdir/copy, not after."""
    import pytest
    from genblaze_core.exceptions import ManifestError
    from genblaze_core.models.enums import PromptVisibility

    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")
    out = tmp_path / "nested" / "redacted.png"

    policy = EmbedPolicy(prompt_visibility=PromptVisibility.PRIVATE)
    handler = SidecarHandler()
    with pytest.raises(ManifestError, match="embed_mode='pointer'"):
        handler.embed(src, sample_manifest, output=out, policy=policy)

    assert not out.exists()
    assert not out.parent.exists(), "rejected policy must not even create output's directory"


def test_embed_distinct_output_copies_media(tmp_path: Path, sample_manifest: Manifest) -> None:
    """A distinct output= must materialize real media bytes there, not just
    the sidecar (#238) — sidecar mode doesn't rewrite bytes, but callers who
    get back `output` as the result path need a real file at that path."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png bytes")
    out = tmp_path / "renamed.png"

    handler = SidecarHandler()
    sidecar = handler.embed(src, sample_manifest, output=out)

    assert out.exists()
    assert out.read_bytes() == src.read_bytes()
    assert sidecar == out.with_suffix(out.suffix + ".genblaze.json")
    assert sidecar.exists()


def test_embed_distinct_output_creates_missing_parent_dir(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """output= in a not-yet-created directory must have its parent created,
    same as the inline handlers' atomic_write does implicitly via mkdir."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png bytes")
    out = tmp_path / "nested" / "deeper" / "renamed.png"

    handler = SidecarHandler()
    handler.embed(src, sample_manifest, output=out)

    assert out.exists()
    assert out.read_bytes() == src.read_bytes()


def test_embed_distinct_output_overwrites_existing_file(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """Overwrite semantics must match the inline handlers: an existing file
    at output= is replaced with the source's bytes, not left alone."""
    src = tmp_path / "image.png"
    src.write_bytes(b"new bytes")
    out = tmp_path / "renamed.png"
    out.write_bytes(b"stale bytes that should be replaced")

    handler = SidecarHandler()
    handler.embed(src, sample_manifest, output=out)

    assert out.read_bytes() == b"new bytes"


def test_embed_output_same_as_source_skips_copy(tmp_path: Path, sample_manifest: Manifest) -> None:
    """When output resolves to the same file as source (including via a
    relative spelling), no copy is needed — only the sidecar is written."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")

    handler = SidecarHandler()
    # Same file, spelled differently (relative vs absolute).
    handler.embed(src, sample_manifest, output=tmp_path / "." / "image.png")

    assert src.read_bytes() == b"fake png"  # untouched, not truncated by a self-copy


def test_embed_distinct_output_follows_symlinked_source(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """A symlinked source must resolve to real bytes at output=, not a
    dangling or symlinked copy."""
    real = tmp_path / "real.png"
    real.write_bytes(b"real bytes")
    src = tmp_path / "link.png"
    src.symlink_to(real)
    out = tmp_path / "renamed.png"

    handler = SidecarHandler()
    handler.embed(src, sample_manifest, output=out)

    assert out.exists()
    assert not out.is_symlink()
    assert out.read_bytes() == b"real bytes"


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


def test_embed_distinct_output_copy_uses_mmap_ceiling_not_file_ceiling(
    tmp_path: Path, sample_manifest: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The output= copy must be a streaming copy bounded by
    MAX_MMAP_BYTES (2GB, Mp4Handler's own ceiling), not read_media_bytes's
    smaller MAX_FILE_BYTES (500MB) — Mp4Handler's error message explicitly
    tells callers larger-than-500MB files should "use sidecar fallback", so
    silently re-imposing the 500MB cap here would break that escape hatch
    (#238 follow-up). Patches the module's copy-size cap down to a few bytes
    so the boundary is testable without allocating real gigabytes."""
    import genblaze_core.media.sidecar as sidecar_module

    monkeypatch.setattr(sidecar_module, "_MAX_COPY_BYTES", 8)

    src = tmp_path / "image.png"
    src.write_bytes(b"small")  # 5 bytes, under the patched 8-byte cap
    out = tmp_path / "renamed.png"
    SidecarHandler().embed(src, sample_manifest, output=out)
    assert out.read_bytes() == b"small"

    src.write_bytes(b"too many bytes")  # over the patched 8-byte cap
    out2 = tmp_path / "renamed2.png"
    with pytest.raises(EmbeddingError, match="File too large for sidecar copy"):
        SidecarHandler().embed(src, sample_manifest, output=out2)
    assert not out2.exists()


def test_pointer_sidecar_distinct_output_creates_media(
    tmp_path: Path, sample_manifest: Manifest
) -> None:
    """Regression for #238: pointer mode + a distinct output= used to write
    only the pointer sidecar, reporting an output path whose media file
    never existed."""
    src = tmp_path / "image.png"
    src.write_bytes(b"fake png")
    out = tmp_path / "redacted.png"

    sample_manifest.manifest_uri = "https://storage.example.com/manifest.json"
    policy = EmbedPolicy(embed_mode="pointer")

    handler = SidecarHandler()
    sidecar = handler.embed(src, sample_manifest, output=out, policy=policy)

    assert out.exists(), "pointer mode must materialize the media at a distinct output"
    assert out.read_bytes() == src.read_bytes()
    assert sidecar == out.with_suffix(out.suffix + ".genblaze.json")
    assert sidecar.exists()
