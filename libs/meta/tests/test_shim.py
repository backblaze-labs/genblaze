"""Parity tests for the genblaze -> genblaze_core pass-through shim."""

from __future__ import annotations

import subprocess
import sys

import genblaze
import genblaze_core
import pytest

# `__version__` is intentionally the meta-package version, not core's.
_INTENTIONALLY_DIVERGENT = {"__version__"}


def test_reexports_are_identity():
    for name in genblaze_core.__all__:
        if name in _INTENTIONALLY_DIVERGENT:
            continue
        assert hasattr(genblaze, name), f"genblaze missing {name!r}"
        assert getattr(genblaze, name) is getattr(genblaze_core, name), (
            f"genblaze.{name} is not the same object as genblaze_core.{name}"
        )


def test_version_is_own_not_cores():
    assert genblaze.__version__ != genblaze_core.__version__, (
        "genblaze.__version__ should be the meta-package version, not core's"
    )


def test_dunder_all_mirrors_core():
    assert set(genblaze.__all__) == set(genblaze_core.__all__)


def test_dir_surfaces_public_symbols():
    listing = dir(genblaze)
    for name in genblaze_core.__all__:
        assert name in listing, f"dir(genblaze) missing {name!r}"


def test_unknown_attribute_error_is_actionable():
    with pytest.raises(AttributeError) as excinfo:
        _ = genblaze.SoraProvider  # type: ignore[attr-defined]
    msg = str(excinfo.value)
    assert "SoraProvider" in msg
    assert "genblaze_openai" in msg


def test_private_names_raise_cleanly():
    with pytest.raises(AttributeError):
        _ = genblaze._does_not_exist  # type: ignore[attr-defined]


def test_star_import_exposes_every_public_symbol():
    ns: dict = {}
    exec("from genblaze import *", ns)  # noqa: S102
    for name in genblaze_core.__all__:
        assert name in ns, f"star import missing {name!r}"
    assert ns["Pipeline"] is genblaze_core.Pipeline


def test_import_genblaze_does_not_eagerly_load_core_submodules():
    """Documents the lazy-load contract: `import genblaze` must not pull
    pipeline / providers / storage / etc. into sys.modules. Regression guard
    for anyone tempted to replace the lazy __getattr__ with eager `from X import *`."""
    code = (
        "import sys, genblaze; "
        "leaked = sorted(m for m in sys.modules "
        "if m.startswith('genblaze_core.') and m != 'genblaze_core._version'); "
        "print(repr(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = eval(result.stdout.strip())  # noqa: S307
    assert leaked == [], f"expected no submodule leak, got: {leaked}"
