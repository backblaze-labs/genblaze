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


def test_dir_surfaces_lazy_names_before_first_access() -> None:
    """Lazy public names must be discoverable without importing their modules."""
    script = (
        "import genblaze_core;"
        "assert 'Pipeline' not in genblaze_core.__dict__;"
        "assert 'Pipeline' in dir(genblaze_core)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_runnable_config_is_a_top_level_export() -> None:
    """RunnableConfig is part of the public runnable API."""
    from genblaze_core import RunnableConfig
    from genblaze_core.runnable.config import RunnableConfig as NestedRunnableConfig

    assert RunnableConfig is NestedRunnableConfig


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


def test_testing_module_importable_without_pytest() -> None:
    """``genblaze_core.testing`` must import on a runtime-only install.

    The mocks moved to the pytest-free ``genblaze_core.mocks`` in 0.3.5, but
    ``genblaze_core.testing`` kept a module-level ``import pytest`` for
    ``ProviderComplianceTests`` — so the documented re-export path (and the
    zero-API-key quickstart in ``libs/core/README.md``) still died with
    ``ModuleNotFoundError: No module named 'pytest'`` on a clean
    ``pip install genblaze-core``. Same subprocess technique as the test
    above: pytest is blocked, so any module-level import of it fails fast.
    """
    script = (
        "import sys; sys.modules.pop('pytest', None);"
        "sys.modules['pytest'] = None;"
        # The line printed by libs/core/README.md's quickstart.
        "from genblaze_core.testing import MockVideoProvider;"
        "from genblaze_core.testing import MockProvider, MockAudioProvider;"
        "from genblaze_core.testing import ProviderComplianceTests;"
        "assert MockVideoProvider is not None;"
        "assert ProviderComplianceTests is not None;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"genblaze_core.testing requires pytest at import.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_testing_module_runs_a_mock_pipeline_without_pytest() -> None:
    """The README's zero-API-key quickstart must run end to end without pytest."""
    script = (
        "import sys; sys.modules.pop('pytest', None);"
        "sys.modules['pytest'] = None;"
        "from genblaze_core import Modality, Pipeline;"
        "from genblaze_core.testing import MockVideoProvider;"
        "run, manifest = ("
        "    Pipeline('hello-genblaze')"
        "    .step(MockVideoProvider(), model='mock-v1', prompt='a drone shot',"
        "          modality=Modality.VIDEO)"
        "    .run(raise_on_failure=True)"
        ");"
        "assert manifest.verify_hash();"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"README quickstart fails without pytest.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
