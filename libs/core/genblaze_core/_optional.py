"""Typed errors and helpers for optional dependencies.

Plan 5 Phase 1A — closes the "umbrella ``__getattr__`` walks into
``pyarrow``" footgun (bug #8 in the storage tranche): a fresh
``pip install genblaze`` user who runs ``from genblaze import
ParquetSink`` previously hit ``ModuleNotFoundError: pyarrow`` with
no actionable hint. The typed :class:`OptionalDependencyError`
includes the install incantation in the message and is catchable
via either the typed name or the legacy ``except ImportError:``
pattern (it's a subclass).

Usage in optional-extra-gated modules::

    # libs/core/genblaze_core/sinks/parquet.py
    try:
        import pyarrow as pa
    except ImportError as exc:
        from genblaze_core._optional import OptionalDependencyError
        raise OptionalDependencyError(
            extra="parquet",
            package="pyarrow",
            symbol="ParquetSink",
        ) from exc
"""

from __future__ import annotations


class OptionalDependencyError(ImportError):
    """Raised when an optional-extra-gated symbol is accessed without the
    extra installed.

    Subclass of :class:`ImportError` so legacy ``except ImportError:``
    callers continue to catch it without code changes; the typed name
    lets new callers be more specific (``except
    OptionalDependencyError:``).

    The error message embeds the install incantation —
    ``pip install "genblaze[parquet]"`` for ``ParquetSink``, etc. —
    so the failure mode points users at the fix rather than at a bare
    "module not found" trace.
    """

    def __init__(
        self,
        *,
        extra: str,
        package: str,
        symbol: str | None = None,
    ) -> None:
        self.extra = extra
        self.package = package
        self.symbol = symbol
        target = symbol if symbol else f"the {package} integration"
        super().__init__(
            f"{target} requires the optional dependency {package!r}. "
            f"Install with: pip install 'genblaze[{extra}]' "
            f"(or pip install {package} directly)."
        )


def require(extra: str, package: str, symbol: str | None = None) -> None:
    """Raise :class:`OptionalDependencyError` if ``package`` isn't importable.

    Convenience helper for module-top-level use:

        from genblaze_core._optional import require
        require(extra="parquet", package="pyarrow", symbol="ParquetSink")
        import pyarrow  # safe — would have raised above if missing

    Or for inline lazy-import patterns inside a function:

        def write_parquet(...):
            require(extra="parquet", package="pyarrow")
            import pyarrow
            ...
    """
    try:
        __import__(package)
    except ImportError as exc:
        raise OptionalDependencyError(extra=extra, package=package, symbol=symbol) from exc


__all__ = ["OptionalDependencyError", "require"]
