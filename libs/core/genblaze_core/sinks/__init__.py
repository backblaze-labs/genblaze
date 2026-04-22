"""Event sinks for persisting run/step data."""

from genblaze_core.sinks.base import BaseSink

__all__ = ["BaseSink", "ParquetSink"]


def __getattr__(name: str):
    """Lazy-load ParquetSink so pyarrow stays optional.

    ParquetSink requires the ``parquet`` extra (``pip install
    "genblaze-core[parquet]"``). Deferring its import until first access
    means merely importing ``genblaze_core.sinks`` does not require
    pyarrow to be installed.
    """
    if name == "ParquetSink":
        from genblaze_core.sinks.parquet import ParquetSink

        return ParquetSink
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
