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


class TestParquetSinkOptionalDep:
    """End-to-end: importing ParquetSink without pyarrow raises
    OptionalDependencyError with a useful message.

    pyarrow IS available in this dev env, so we simulate its absence
    by evicting it from ``sys.modules`` and shadowing it with a
    finder that fails. This mirrors how a fresh-install user without
    the parquet extra would experience the import.
    """

    def test_parquet_sink_module_raises_typed_error_when_pyarrow_missing(self):
        # Evict any cached imports.
        for cached in list(sys.modules):
            if cached.startswith("pyarrow") or cached == "genblaze_core.sinks.parquet":
                sys.modules.pop(cached, None)

        # Block any future ``import pyarrow`` by assigning None into sys.modules.
        # Python's import machinery treats this as "pyarrow is shadowed and
        # cannot be imported" — re-importing parquet.py now fails the
        # ``import pyarrow`` line at the top.
        sys.modules["pyarrow"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(OptionalDependencyError) as exc_info:
                import genblaze_core.sinks.parquet  # noqa: F401
            err = exc_info.value
            assert err.extra == "parquet"
            assert err.package == "pyarrow"
            assert err.symbol == "ParquetSink"
            # And the legacy except-ImportError path catches it.
            assert isinstance(err, ImportError)
        finally:
            # Restore a clean state for downstream tests in the same session.
            sys.modules.pop("pyarrow", None)
            sys.modules.pop("genblaze_core.sinks.parquet", None)
