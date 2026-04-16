"""StepSpan — lightweight timing context manager for steps.

Optionally bridges to OpenTelemetry when the ``opentelemetry`` package is installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any


@dataclass
class StepSpan:
    """Captures timing and metadata for a step execution."""

    name: str
    run_id: str | None = None
    step_id: str | None = None
    retries: int = 0
    cost: float | None = None
    start_time: float = 0.0
    end_time: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    _otel_span: Any = field(default=None, repr=False)

    def __enter__(self) -> StepSpan:
        self.start_time = time.monotonic()
        # Start an OTel span if the SDK is available (soft dependency)
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer("genblaze")
            self._otel_span = tracer.start_span(self.name)
            if self.step_id:
                self._otel_span.set_attribute("genblaze.step_id", self.step_id)
            if self.run_id:
                self._otel_span.set_attribute("genblaze.run_id", self.run_id)
        except ImportError:
            pass
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.end_time = time.monotonic()
        # End the OTel span with final attributes
        if self._otel_span is not None:
            try:
                self._otel_span.set_attribute("genblaze.duration_ms", self.duration_ms)
                self._otel_span.set_attribute("genblaze.retries", self.retries)
                if self.cost is not None:
                    self._otel_span.set_attribute("genblaze.cost_usd", self.cost)
                # Record exception if one occurred
                if exc_val is not None:
                    self._otel_span.record_exception(exc_val)
                    from opentelemetry.trace import StatusCode

                    self._otel_span.set_status(StatusCode.ERROR, str(exc_val))
                self._otel_span.end()
            except Exception:  # noqa: S110
                pass

    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000
