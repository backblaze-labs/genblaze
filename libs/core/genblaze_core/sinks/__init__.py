"""Event sinks for persisting run/step data."""

from genblaze_core.sinks.base import BaseSink
from genblaze_core.sinks.parquet import ParquetSink

__all__ = ["BaseSink", "ParquetSink"]
