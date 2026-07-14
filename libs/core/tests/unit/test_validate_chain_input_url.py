"""Security tests for ``validate_chain_input_url``.

Threat model: a pipeline accepts a user-controlled ``step.inputs`` URL
(via a public API, queue worker, CLI flag, etc.). Without
canonicalization, ``file:///etc/passwd`` and percent-encoded
traversal variants reach providers that forward the URL to ffmpeg /
audio decoders / subprocesses — local file disclosure.

The function ships in two modes:
- **default**: best-effort — denylist-by-canonical-prefix + RFC netloc
  check + ``..`` collapse via ``Path.resolve``. Documented as
  non-exhaustive; deployments accepting user-controlled URLs MUST
  pass ``file_root_allowlist``.
- **strict** (``file_root_allowlist`` provided): every accepted path
  must canonicalize under one of the listed roots; symlinks resolve
  through.

This corpus pins the contract for both modes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.providers.base import validate_chain_input_url

# ---------------------------------------------------------------------------
# Default mode — accepted URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # https
        "https://example.com/foo.mp4",
        "https://cdn.example.com/path/to/asset.png",
        "https://example.com/percent%20space.mp4",  # benign percent-encoding
        # file:// — basic
        "file:///tmp/output.mp4",
        "file://localhost/tmp/output.mp4",  # RFC 8089 alias
        # file:// — `..` contained within a non-sensitive prefix; resolves
        # to a benign canonical path. Default mode accepts; allowlist mode
        # would reject if /valid is not allowlisted.
        "file:///valid/path/../safe/output.mp4",
        # file:// — percent-encoded `..` that stays within a non-sensitive
        # prefix after unquote+resolve.
        "file:///valid/path/..%2Fsafe%2Foutput.mp4",
    ],
)
def test_default_mode_accepts(url: str) -> None:
    validate_chain_input_url(url)


# ---------------------------------------------------------------------------
# Default mode — rejected URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Disallowed schemes
        "http://example.com/foo",
        "ftp://example.com/foo",
        "ssh://user@host/path",
        "data:text/plain,foo",
        # https with empty host
        "https:///foo.mp4",
        # file:// with non-empty / non-localhost netloc
        "file://remote-host/tmp/foo",
        "file://192.168.1.1/share/file",
        # file:// with relative path (after url parsing)
        "file://relative/path",
        # Sensitive system paths (denylist hits via canonicalize)
        "file:///etc/passwd",
        "file:///etc/shadow",
        "file:///proc/self/cmdline",
        "file:///proc/1/environ",
        "file:///dev/null",
        "file:///dev/zero",
        "file:///sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
        # Container / Kubernetes secrets
        "file:///run/secrets/app-token",
        "file:///var/run/secrets/kubernetes.io/serviceaccount/token",
        # Single-pass percent-encoded traversal that escapes to a denylisted path
        "file:///foo/..%2Fetc%2Fpasswd",  # → /etc → denylist
        # Plain `..` traversal that escapes to a denylisted path
        "file:///foo/../etc/passwd",  # → /etc → denylist
    ],
)
def test_default_mode_rejects(url: str) -> None:
    with pytest.raises(ProviderError):
        validate_chain_input_url(url)


def test_macos_private_etc_alias_rejected() -> None:
    """On macOS, ``/etc`` is a symlink to ``/private/etc``. The denylist
    must catch both forms via canonicalize."""
    # Both should be rejected; the denylist includes both prefixes.
    with pytest.raises(ProviderError):
        validate_chain_input_url("file:///private/etc/passwd")
    with pytest.raises(ProviderError):
        validate_chain_input_url("file:///private/var/run/foo")


# ---------------------------------------------------------------------------
# Strict mode — file_root_allowlist
# ---------------------------------------------------------------------------


def test_allowlist_accepts_path_under_root(tmp_path: Path) -> None:
    inside = tmp_path / "asset.mp4"
    validate_chain_input_url(
        f"file://{inside}",
        file_root_allowlist=(tmp_path,),
    )


def test_allowlist_rejects_path_outside_root(tmp_path: Path) -> None:
    other_root = Path(tempfile.mkdtemp())
    try:
        other = other_root / "other-app" / "asset.mp4"
        with pytest.raises(ProviderError, match="not under any allowlisted root"):
            validate_chain_input_url(
                f"file://{other}",
                file_root_allowlist=(tmp_path,),
            )
    finally:
        os.rmdir(other_root)


def test_allowlist_rejects_traversal_escape(tmp_path: Path) -> None:
    """Even if a URL appears to be under the allowlist root, ``..`` that
    escapes must be rejected after canonicalization."""
    escape_url = f"file://{tmp_path}/../../../etc/passwd"
    with pytest.raises(ProviderError):
        validate_chain_input_url(
            escape_url,
            file_root_allowlist=(tmp_path,),
        )


def test_allowlist_rejects_outside_root_symlink(tmp_path: Path) -> None:
    """A symlink inside the allowlist root pointing OUTSIDE must be
    rejected. ``Path.resolve`` follows the symlink before the
    containment check, so the resolved path is the symlink target."""
    sensitive_target = Path(tempfile.mkdtemp()) / "secret.txt"
    sensitive_target.write_text("secret")
    try:
        link = tmp_path / "link"
        link.symlink_to(sensitive_target)
        with pytest.raises(ProviderError, match="not under any allowlisted root"):
            validate_chain_input_url(
                f"file://{link}",
                file_root_allowlist=(tmp_path,),
            )
    finally:
        sensitive_target.unlink(missing_ok=True)
        os.rmdir(sensitive_target.parent)


def test_allowlist_accepts_inside_root_symlink(tmp_path: Path) -> None:
    """Symlinks pointing INSIDE the allowlist root should still be
    accepted."""
    real = tmp_path / "real.mp4"
    real.write_bytes(b"")
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    validate_chain_input_url(
        f"file://{link}",
        file_root_allowlist=(tmp_path,),
    )


def test_multiple_allowlist_roots(tmp_path: Path) -> None:
    """Multiple roots in the allowlist — path under any one is accepted."""
    other_root = Path(tempfile.mkdtemp())
    try:
        validate_chain_input_url(
            f"file://{other_root / 'asset.mp4'}",
            file_root_allowlist=(tmp_path, other_root),
        )
    finally:
        os.rmdir(other_root)


# ---------------------------------------------------------------------------
# Documented limitations — multi-pass percent-encoding in default mode
# ---------------------------------------------------------------------------


def test_double_encoded_default_mode_accepts_literal(tmp_path: Path) -> None:
    """``%252F`` is double-encoded — single-pass ``unquote`` decodes it
    to literal ``%2F``, which is a filename character (not a path
    separator). The resolved path stays under ``/valid/path/`` and is
    accepted in default mode.

    This is a documented limitation: deployments accepting user-supplied
    URLs must use ``file_root_allowlist`` (which the next test pins).
    """
    validate_chain_input_url("file:///valid/path/..%252Fsafe%252Foutput.mp4")


def test_double_encoded_strict_mode_rejects(tmp_path: Path) -> None:
    """In strict mode, the double-encoded path doesn't sit under the
    allowlist root, so the containment check rejects it."""
    with pytest.raises(ProviderError):
        validate_chain_input_url(
            "file:///valid/path/..%252Fsafe%252Foutput.mp4",
            file_root_allowlist=(tmp_path,),
        )


def test_windows_drive_letter_file_url(tmp_path: Path, monkeypatch) -> None:
    """Regression for #132: url2pathname() strips the leading slash before a
    Windows drive letter so Path.is_absolute() passes and Path.resolve()
    produces the correct canonical path. Simulates Windows url2pathname."""
    asset = tmp_path / "asset.mp4"
    real_path = str(asset.resolve())
    monkeypatch.setattr(
        "genblaze_core.providers.base.url2pathname",
        lambda _: real_path,
    )
    # Should not raise — the path passes the is_absolute() check and
    # (with allowlist) the containment check when url2pathname gives back
    # the correct Windows-resolved path.
    validate_chain_input_url(
        "file:///C:/tmp/asset.mp4",
        file_root_allowlist=(tmp_path,),
    )
