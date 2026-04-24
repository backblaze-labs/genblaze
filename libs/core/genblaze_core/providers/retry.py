"""Retry primitives shared by every BaseProvider phase (submit/poll/fetch).

Kept intentionally small — this is a policy toolbox, not a framework. Knobs
stay as class attributes on ``BaseProvider`` until evidence shows they need
a real config object.
"""

from __future__ import annotations

from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

from genblaze_core._utils import utc_now

# Upper bound on a server-supplied Retry-After hint. A misconfigured or hostile
# upstream should not be able to freeze the pipeline for minutes. 120s is long
# enough to honor real rate-limit windows (OpenAI, Anthropic, Replicate) while
# short enough that the global ``config.timeout`` (default 600s) still bites.
MAX_RETRY_AFTER_SEC: float = 120.0


def _pre_response_exceptions() -> tuple[type[BaseException], ...]:
    """Return the httpx exception classes that are safe to retry on submit.

    Pre-response means the request never reached the server (or never completed
    transmission), so retrying cannot double-trigger a side effect. We exclude
    ``ReadTimeout`` and ``WriteTimeout``: the request may have been processed
    server-side, and retrying without an idempotency key could double-bill.

    Resolved lazily so ``genblaze-core`` can import cleanly without httpx.
    """
    try:
        import httpx
    except ImportError:
        return ()
    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


PRE_RESPONSE_EXCEPTIONS: tuple[type[BaseException], ...] = _pre_response_exceptions()


def retry_after_from_response(resp: Any) -> float | None:
    """Parse a ``Retry-After`` header into seconds, clamped to the safety cap.

    Accepts any of: a response object with ``.headers`` (``httpx.Response``,
    ``requests.Response``), an SDK exception that wraps one on ``.response``
    (``openai.APIStatusError``, ``httpx.HTTPStatusError``), or a plain headers
    mapping. Values may be delta-seconds or an HTTP-date (RFC 7231 §7.1.3).
    Returns ``None`` if the header is absent, malformed, or in the past.
    """
    if resp is None:
        return None
    # Unwrap SDK exception wrappers that carry the response on ``.response``.
    # Safe even when ``resp`` is already a response — getattr returns ``resp``.
    resp = getattr(resp, "response", resp)
    headers = resp if isinstance(resp, Mapping) else getattr(resp, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    # Delta-seconds — the overwhelmingly common case.
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None
    if seconds is None:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        seconds = (dt - utc_now()).total_seconds()
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SEC)


__all__ = [
    "MAX_RETRY_AFTER_SEC",
    "PRE_RESPONSE_EXCEPTIONS",
    "retry_after_from_response",
]
