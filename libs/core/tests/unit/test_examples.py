"""Regression tests for the zero-dependency scripts under ``examples/``.

``examples/quickstart_local.py`` is the first thing a new user runs (per its
own docstring and the README quickstart link) — it must not print
``Verified: False``, which reads as a broken install. See issue #125:
`Manifest.verify()` was hardened in genblaze-core 0.3.4 to require a
``sha256`` on every output asset, and the example wasn't updated to supply
one for its synthetic (no real bytes) demo asset.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_EXAMPLES_DIR = Path(__file__).resolve().parents[4] / "examples"

# Tolerates the label's column-alignment whitespace so a cosmetic reformat of
# the print statement doesn't break this regression guard on unrelated grounds.
_VERIFIED_TRUE = re.compile(r"Verified:\s*True")


def test_quickstart_local_reports_verified_true() -> None:
    """Running quickstart_local.py end-to-end must report a verified manifest.

    Runs in a fresh subprocess (same interpreter, so the editable install
    resolves) so this exercises exactly what a user copy-pasting the
    README's `python examples/quickstart_local.py` command would see.
    """
    script = _EXAMPLES_DIR / "quickstart_local.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert _VERIFIED_TRUE.search(result.stdout), (
        f"Expected a verified manifest from the zero-key quickstart.\nstdout: {result.stdout}"
    )
