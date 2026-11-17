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


@pytest.fixture
def missing_pyarrow():
    """Simulate the ``parquet`` extra not being installed.

    pyarrow IS available in this dev env, so we evict any cached import and
    shadow it in ``sys.modules`` with ``None`` — Python's import machinery
    treats that as "this module cannot be imported," mirroring what a
    fresh-install user without the parquet extra would experience. Mirrors
    the fixture of the same name in
    ``libs/core/tests/unit/test_optional_imports.py``.
    """
    for cached in list(sys.modules):
        if cached.startswith("pyarrow") or cached == "genblaze_core.sinks.parquet":
            sys.modules.pop(cached, None)
    sys.modules["pyarrow"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.modules.pop("pyarrow", None)
        sys.modules.pop("genblaze_core.sinks.parquet", None)


class TestLazyAttributeCapabilityProbing:
    """Issue #197: the umbrella ``genblaze.__getattr__`` must preserve the
    actionable ``OptionalDependencyError`` install hint that
    ``genblaze_core.__getattr__`` raises (issue #165) instead of collapsing
    it into the generic "unknown attribute / provider adapter" message.
    Mirrors ``TestLazyAttributeCapabilityProbing`` in
    ``libs/core/tests/unit/test_optional_imports.py``.
    """

    @staticmethod
    def _evict_cached_lazy_attr(name: str) -> None:
        # Both genblaze and genblaze_core cache resolved lazy imports into
        # module globals; pop from both so __getattr__ runs again instead of
        # returning a class resolved by an earlier, unrelated test/import.
        genblaze.__dict__.pop(name, None)
        genblaze_core.__dict__.pop(name, None)

    def test_hasattr_returns_false_when_optional_dep_missing(self, missing_pyarrow):
        self._evict_cached_lazy_attr("ParquetSink")
        assert hasattr(genblaze, "ParquetSink") is False

    def test_direct_access_surfaces_install_hint(self, missing_pyarrow):
        self._evict_cached_lazy_attr("ParquetSink")
        with pytest.raises(AttributeError) as excinfo:
            _ = genblaze.ParquetSink  # type: ignore[attr-defined]
        msg = str(excinfo.value)
        assert "pyarrow" in msg
        assert "pip install 'genblaze[parquet]'" in msg
        assert "ParquetSink" in msg
        # The misleading "provider adapter" fallback must not appear.
        assert "provider adapter" not in msg

    def test_genuinely_unknown_name_still_gets_fallback_message(self):
        # Doesn't touch ParquetSink/pyarrow — no missing_pyarrow fixture needed.
        with pytest.raises(AttributeError) as excinfo:
            _ = genblaze.SoraProvider  # type: ignore[attr-defined]
        msg = str(excinfo.value)
        assert "SoraProvider" in msg
        assert "genblaze_openai" in msg


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
