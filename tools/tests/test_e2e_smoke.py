"""Tests for tools/e2e_smoke.py: it passes on a healthy install and can fail."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import e2e_smoke  # noqa: E402

requires_cli = pytest.mark.skipif(
    shutil.which("genblaze") is None, reason="genblaze CLI not installed"
)


@requires_cli
def test_smoke_passes_on_healthy_install(capsys):
    assert e2e_smoke.main() == 0
    assert "OK: e2e smoke passed" in capsys.readouterr().out


@requires_cli
def test_smoke_fails_if_verify_accepts_tampered_asset(monkeypatch, tmp_path):
    real_cli = e2e_smoke._cli
    calls = []

    def lenient_cli(*args):
        calls.append(args[0])
        if len(calls) == 3:  # the post-tamper verify
            return subprocess.CompletedProcess(args, 0, "OK", "")
        return real_cli(*args)

    monkeypatch.setattr(e2e_smoke, "_cli", lenient_cli)
    with pytest.raises(e2e_smoke.SmokeFailure, match="tampered"):
        e2e_smoke.run_smoke(tmp_path.resolve())


def test_smoke_fails_clearly_without_cli(monkeypatch, capsys):
    monkeypatch.setattr(e2e_smoke.shutil, "which", lambda _: None)
    assert e2e_smoke.main() == 1
    assert "make install-dev" in capsys.readouterr().err
