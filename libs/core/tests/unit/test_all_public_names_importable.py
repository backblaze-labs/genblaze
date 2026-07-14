"""Smoke test: every name in genblaze_core.__all__ must resolve without error.

This is the recurrence guard for Fix A — a top-level import dep (like pytest)
hiding inside a public-API module would have been caught here before release.

Two checks:
1. In-process: iterate __all__ and call getattr. Verifies the lazy-import
   dispatch table has no broken entries.
2. Subprocess: import genblaze_core.MockVideoProvider in a fresh interpreter
   with pytest evicted from sys.path. Proves the no-pytest guarantee holds
   at the interpreter boundary, not just inside this test process.
"""

from __future__ import annotations

import subprocess
import sys

import genblaze_core


def test_all_public_names_resolve() -> None:
    """Every name in __all__ must be retrievable without AttributeError."""
    failures = []
    for name in genblaze_core.__all__:
        try:
            getattr(genblaze_core, name)
        except Exception as exc:
            failures.append(f"{name}: {exc}")
    assert not failures, "Names in __all__ failed to resolve:\n" + "\n".join(failures)


def test_mock_providers_importable_without_pytest() -> None:
    """MockProvider / MockVideoProvider / MockAudioProvider must not require pytest.

    Runs in a fresh subprocess so pytest's presence in the current process
    does not mask a hidden dependency. Uses sys.executable to target the
    same interpreter (and therefore the same editable install) as the test suite.
    """
    script = (
        "import sys; sys.modules.pop('pytest', None);"
        # Block pytest from being importable — any import of it will fail fast.
        "import unittest.mock; sys.modules['pytest'] = None;"
        "import genblaze_core;"
        "assert genblaze_core.MockProvider is not None;"
        "assert genblaze_core.MockVideoProvider is not None;"
        "assert genblaze_core.MockAudioProvider is not None;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Mock providers require pytest at import.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
