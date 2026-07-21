"""Tests for ``OptionalDependencyError`` and the optional-extra-gated
import path.

The bug being closed (storage tranche bug #8): pre-fix, a fresh
``pip install genblaze`` user running ``from genblaze import
ParquetSink`` got ``ModuleNotFoundError: pyarrow`` with no actionable
hint. Post-fix, the typed :class:`OptionalDependencyError` carries
the install incantation in its message.

Subclass-of-ImportError contract: legacy ``except ImportError:``
catches the typed error too — no breaking change for existing
callers.
"""

from __future__ import annotations

import sys

import pytest
from genblaze_core._optional import OptionalDependencyError, require


class TestOptionalDependencyError:
    def test_is_subclass_of_import_error(self):
        """Legacy ``except ImportError:`` catches OptionalDependencyError —
        contract is load-bearing for existing code that handles missing
        optional deps generically."""
        assert issubclass(OptionalDependencyError, ImportError)

    def test_is_not_subclass_of_attribute_error(self):
        """Issue #165: CPython forbids a single class from subclassing both
        ImportError and AttributeError (``TypeError: multiple bases have
        instance lay-out conflict`` — both gained C-level slots in 3.10+).
        So this type stays a pure ImportError; ``genblaze_core.__getattr__``
        is responsible for converting it to AttributeError at the lazy-
        attribute-resolution call site (see TestLazyAttributeCapabilityProbing)."""
        assert not issubclass(OptionalDependencyError, AttributeError)

    def test_message_includes_install_incantation(self):
        err = OptionalDependencyError(extra="parquet", package="pyarrow", symbol="ParquetSink")
        msg = str(err)
        assert "pyarrow" in msg
        assert "pip install 'genblaze[parquet]'" in msg
        assert "ParquetSink" in msg

    def test_message_falls_back_to_package_when_no_symbol(self):
        err = OptionalDependencyError(extra="async", package="aioboto3")
        msg = str(err)
        assert "the aioboto3 integration" in msg

    def test_attributes_preserved(self):
        err = OptionalDependencyError(extra="parquet", package="pyarrow", symbol="ParquetSink")
        assert err.extra == "parquet"
        assert err.package == "pyarrow"
        assert err.symbol == "ParquetSink"


class TestRequireHelper:
    def test_passes_when_module_importable(self):
        # ``json`` is in the stdlib — always importable.
        require(extra="any", package="json")  # no raise

    def test_raises_optional_dep_error_when_missing(self):
        with pytest.raises(OptionalDependencyError) as exc_info:
            require(
                extra="some-extra",
                package="this_module_definitely_does_not_exist",
                symbol="SomeFakeSymbol",
            )
        err = exc_info.value
        assert err.extra == "some-extra"
        assert err.package == "this_module_definitely_does_not_exist"
        assert err.symbol == "SomeFakeSymbol"

    def test_caught_by_legacy_except_ImportError(self):
        try:
            require(extra="x", package="this_module_does_not_exist")
        except ImportError as exc:
            # Legacy code expects ImportError — must catch the typed
            # subclass without code changes.
            assert isinstance(exc, OptionalDependencyError)
        else:
            pytest.fail("expected ImportError-shaped exception")


@pytest.fixture
def missing_pyarrow():
    """Simulate the ``parquet`` extra not being installed.

    pyarrow IS available in this dev env, so we evict any cached import
    and shadow it in ``sys.modules`` with ``None`` — Python's import
    machinery treats that as "this module cannot be imported," mirroring
    what a fresh-install user without the parquet extra would experience.
    """
    for cached in list(sys.modules):
        if cached.startswith("pyarrow") or cached == "genblaze_core.sinks.parquet":
            sys.modules.pop(cached, None)
    sys.modules["pyarrow"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        # Restore a clean state for downstream tests in the same session.
        sys.modules.pop("pyarrow", None)
        sys.modules.pop("genblaze_core.sinks.parquet", None)


class TestParquetSinkOptionalDep:
    """End-to-end: importing ParquetSink without pyarrow raises
    OptionalDependencyError with a useful message.
    """

    def test_parquet_sink_module_raises_typed_error_when_pyarrow_missing(self, missing_pyarrow):
        with pytest.raises(OptionalDependencyError) as exc_info:
            import genblaze_core.sinks.parquet  # noqa: F401
        err = exc_info.value
        assert err.extra == "parquet"
        assert err.package == "pyarrow"
        assert err.symbol == "ParquetSink"
        # And the legacy except-ImportError path catches it.
        assert isinstance(err, ImportError)


class TestLazyAttributeCapabilityProbing:
    """Issue #165: ``genblaze_core.__getattr__`` (the umbrella-package lazy
    import table) must let ``hasattr``/``getattr(..., default)`` probe a
    lazy-imported symbol whose optional extra isn't installed without
    crashing — while a consumer who actually *uses* the symbol still gets
    the actionable install hint, not a bare AttributeError.
    """

    @staticmethod
    def _evict_cached_lazy_attr(name: str) -> None:
        # __getattr__ caches a resolved lazy import into module globals
        # (see genblaze_core/__init__.py: ``globals()[name] = val``);
        # pop the cache so __getattr__ runs again instead of returning
        # the class resolved by an earlier, unrelated test/import.
        import genblaze_core

        genblaze_core.__dict__.pop(name, None)

    def test_hasattr_returns_false_when_optional_dep_missing(self, missing_pyarrow):
        import genblaze_core

        self._evict_cached_lazy_attr("ParquetSink")
        assert hasattr(genblaze_core, "ParquetSink") is False

    def test_getattr_with_default_returns_default_when_optional_dep_missing(self, missing_pyarrow):
        import genblaze_core

        self._evict_cached_lazy_attr("ParquetSink")
        sentinel = object()
        assert getattr(genblaze_core, "ParquetSink", sentinel) is sentinel

    def test_direct_access_still_raises_with_install_hint(self, missing_pyarrow):
        """Real usage (not probing) must still surface the actionable
        install hint. The raised type is AttributeError (required for
        correct __getattr__ semantics — see module docstring), but the
        message is the original OptionalDependencyError's, and the typed
        error is still reachable via ``__cause__`` for callers that want
        to branch on it specifically."""
        import genblaze_core

        self._evict_cached_lazy_attr("ParquetSink")
        with pytest.raises(AttributeError) as exc_info:
            _ = genblaze_core.ParquetSink
        msg = str(exc_info.value)
        assert "pyarrow" in msg
        assert "pip install 'genblaze[parquet]'" in msg
        assert "ParquetSink" in msg
        assert isinstance(exc_info.value.__cause__, OptionalDependencyError)
