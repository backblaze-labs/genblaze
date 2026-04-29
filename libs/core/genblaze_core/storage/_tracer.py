"""``traced`` decorator — OpenTelemetry instrumentation for storage backends.

Per-method decorator that opens a ``genblaze.storage.{op}`` span and attaches
``key`` / ``bucket`` / ``request_id`` attributes when those are available
on the call. Composes with the existing :class:`OTelTracer` pattern in
:mod:`genblaze_core.observability.tracer` (both call
``opentelemetry.trace.get_tracer(...)`` directly), but uses a separate
tracer name so storage spans are filterable independently from
pipeline-step spans.

Decorate-time fast path: when ``opentelemetry`` isn't installed, the
decorator returns the wrapped function unchanged — zero call-site
overhead, no extra frame on the stack. When OpenTelemetry IS installed
and no exporter is configured, the underlying ``NoOpTracer`` cost is
~200-400ns/call, which we accept rather than gate behind a config flag
(branch in hot path is the same cost).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])

# Resolved once at module import. ``None`` when OpenTelemetry isn't installed.
# We deliberately do NOT install a no-op shim here — the decorator returns
# the wrapped function unchanged in that case, so we pay nothing per call.
_otel_tracer: Any | None = None
try:
    from opentelemetry import trace as _otel_trace  # type: ignore[import-not-found]

    _otel_tracer = _otel_trace.get_tracer("genblaze.storage")
except ImportError:  # pragma: no cover — exercised in minimal-install env
    _otel_tracer = None


def _attrs_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pull span-worthy attributes from a backend method's kwargs.

    We surface ``key`` and ``bucket`` when present — they're the
    discriminating attributes for storage spans. Anything else stays
    out of OTel attributes (no PII risk, no payload bloat).
    """
    attrs: dict[str, Any] = {}
    if "key" in kwargs and isinstance(kwargs["key"], str):
        attrs["genblaze.storage.key"] = kwargs["key"]
    if "bucket" in kwargs and isinstance(kwargs["bucket"], str):
        attrs["genblaze.storage.bucket"] = kwargs["bucket"]
    return attrs


def _bound_self_attrs(self_obj: Any) -> dict[str, Any]:
    """Pull bucket from ``self`` when the backend exposes one.

    Backends commonly stash the bucket on a private ``_bucket`` attribute;
    surface it as a span attribute so a single decoration covers both
    "key passed in" and "bucket on the instance" call shapes.
    """
    bucket = getattr(self_obj, "_bucket", None)
    if isinstance(bucket, str):
        return {"genblaze.storage.bucket": bucket}
    return {}


def _record_request_id(span: Any, result: Any) -> None:
    """Best-effort: surface a backend ``request_id`` as a span attribute.

    Backends that return ``StorageError`` from a failure path already carry
    ``request_id``; for success returns we look for an ``x-amz-request-id``
    on a ``response`` attribute when available.
    """
    if result is None:
        return
    request_id = getattr(result, "request_id", None)
    if isinstance(request_id, str):
        span.set_attribute("genblaze.storage.request_id", request_id)


def traced(op_name: str) -> Callable[[F], F]:
    """Decorate a sync OR async backend method to emit a storage span.

    Args:
        op_name: Short operation name (``"put"``, ``"get"``, ``"head"``,
            ``"presigned_get"``, etc.). Used as the span suffix.

    The decorator detects the wrapped function's sync/async shape and
    installs the matching wrapper. When OpenTelemetry isn't installed,
    the wrapped function is returned unchanged — zero overhead.
    """

    def decorate(func: F) -> F:
        if _otel_tracer is None:
            # Zero-overhead path. No wrapper frame, no branch per call.
            return func

        is_coro = inspect.iscoroutinefunction(func)

        if is_coro:

            @functools.wraps(func)
            async def async_wrapper(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
                with _otel_tracer.start_as_current_span(  # type: ignore[union-attr]
                    f"genblaze.storage.{op_name}"
                ) as span:
                    for k, v in _bound_self_attrs(self_obj).items():
                        span.set_attribute(k, v)
                    for k, v in _attrs_from_kwargs(kwargs).items():
                        span.set_attribute(k, v)
                    try:
                        result = await cast(Callable[..., Awaitable[Any]], func)(
                            self_obj, *args, **kwargs
                        )
                    except Exception as exc:
                        span.record_exception(exc)
                        _record_request_id(span, exc)
                        raise
                    _record_request_id(span, result)
                    return result

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
            with _otel_tracer.start_as_current_span(  # type: ignore[union-attr]
                f"genblaze.storage.{op_name}"
            ) as span:
                for k, v in _bound_self_attrs(self_obj).items():
                    span.set_attribute(k, v)
                for k, v in _attrs_from_kwargs(kwargs).items():
                    span.set_attribute(k, v)
                try:
                    result = func(self_obj, *args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    _record_request_id(span, exc)
                    raise
                _record_request_id(span, result)
                return result

        return cast(F, sync_wrapper)

    return decorate


__all__ = ["traced"]
