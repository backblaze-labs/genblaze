"""Structured JSON logger for genblaze events."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from genblaze_core._utils import utc_now


class StructuredLogger:
    """Emit JSON-formatted log events."""

    def __init__(
        self,
        name: str = "genblaze",
        level: int = logging.INFO,
        *,
        context: dict[str, Any] | None = None,
    ):
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        self._logger.setLevel(level)
        self._context: dict[str, Any] = context or {}

    def with_context(self, **kwargs: Any) -> StructuredLogger:
        """Return a new logger that auto-includes the given context fields."""
        merged = {**self._context, **kwargs}
        logger = StructuredLogger.__new__(StructuredLogger)
        logger._logger = self._logger
        logger._context = merged
        return logger

    def _emit(self, level: str, event: str, **kwargs: Any) -> None:
        record = {
            "timestamp": utc_now().isoformat(),
            "level": level,
            "event": event,
            **self._context,
            **kwargs,
        }
        self._logger.log(
            getattr(logging, level.upper(), logging.INFO),
            json.dumps(record, default=str),
        )

    def info(self, event: str, **kwargs: Any) -> None:
        self._emit("info", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._emit("error", event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._emit("debug", event, **kwargs)
