"""Retry primitives shared by every BaseProvider phase (submit/poll/fetch).

Phase 1 (0.2.5) shipped utility functions only — `retry_after_from_response`,
`PRE_RESPONSE_EXCEPTIONS`, `MAX_RETRY_AFTER_SEC`. Phase 2 (this file) adds the
``RetryPolicy`` dataclass that callers can pass to ``BaseProvider(retry_policy=...)``
to tune per-instance retry behavior. Defaults match the pre-policy ``BaseProvider``
behavior so existing subclasses (and any consumer that doesn't pass ``retry_policy=``)
keep working unchanged.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from genblaze_core._utils import utc_now
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode

if TYPE_CHECKING:
    from genblaze_core.models.step import Step

logger = logging.getLogger("genblaze.provider.retry")

_T = TypeVar("_T")

# Upper bound on a server-supplied Retry-After hint. A misconfigured or hostile
# upstream should not be able to freeze the pipeline for minutes. 120s is long
# enough to honor real rate-limit windows (OpenAI, Anthropic, Replicate) while
# short enough that the global ``config.timeout`` (default 600s) still bites.
MAX_RETRY_AFTER_SEC: float = 120.0

JitterStrategy = Literal["none", "full", "equal"]
IdempotencyStrategy = Literal["none", "step_id", "uuid_per_attempt"]


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


def _default_retryable_codes() -> frozenset[ProviderErrorCode]:
    return frozenset(
        {
            ProviderErrorCode.TIMEOUT,
            ProviderErrorCode.RATE_LIMIT,
            ProviderErrorCode.SERVER_ERROR,
        }
    )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """User-tunable retry behavior for ``BaseProvider`` lifecycle phases.

    Pass to a provider via ``Provider(retry_policy=RetryPolicy(...))`` or use one of
    the preset classmethods. Defaults reproduce the pre-policy ``BaseProvider``
    behavior: 6 attempts (1 initial + 5 retries), 1s exponential base with full
    jitter, 30s cap, ``Retry-After`` honored, ``step_id`` reused as the idempotency
    key when the provider has opted in by setting ``IDEMPOTENCY_HEADER_NAME``.

    Backoff timing for attempt ``N`` (1-based, attempt 1 is the first retry after
    the initial failure): ``min(initial_backoff * multiplier ** (N-1), max_backoff)``,
    optionally jittered. A server-supplied ``Retry-After`` always wins over the
    computed value when ``respect_retry_after`` is ``True``.

    Frozen + slotted: cheap to share across threads, callers cannot mutate after
    handing to a provider.
    """

    max_attempts: int = 6
    """Total attempts per phase, including the initial one. ``1`` disables retry.

    Default of 6 (1 initial + 5 retries) matches the historical
    ``BaseProvider.poll_transient_retries=5`` knob — so ``RetryPolicy()``
    standalone produces the same behavior as ``BaseProvider`` with no
    explicit policy passed."""

    initial_backoff_sec: float = 1.0
    """Base delay before the first retry. Subject to ``backoff_multiplier`` and ``jitter``."""

    max_backoff_sec: float = 30.0
    """Hard cap on computed delay (does not affect ``Retry-After``, which has its own cap)."""

    backoff_multiplier: float = 2.0
    """Exponential factor between successive retries."""

    jitter: JitterStrategy = "full"
    """``"full"`` (AWS-style ``uniform(0, base)`` — best for de-syncing herds),
    ``"equal"`` (``base/2 + uniform(0, base/2)`` — half-jitter), or ``"none"``."""

    respect_retry_after: bool = True
    """Honor server ``Retry-After`` headers (clamped to ``MAX_RETRY_AFTER_SEC``)."""

    retryable_codes: frozenset[ProviderErrorCode] = field(default_factory=_default_retryable_codes)
    """Normalized error codes that are eligible for retry. Codes outside this set
    fail fast regardless of attempt count. Default: ``TIMEOUT``, ``RATE_LIMIT``,
    ``SERVER_ERROR``. ``CONTENT_POLICY``, ``AUTH_FAILURE``, ``INVALID_INPUT``, and
    ``MODEL_ERROR`` are deterministic and excluded by default."""

    idempotency_key_strategy: IdempotencyStrategy = "step_id"
    """How ``make_idempotency_key`` derives the value sent on retry-eligible submits.
    ``"step_id"`` reuses ``step.step_id`` (UUID, stable across retries — recommended).
    ``"uuid_per_attempt"`` generates a fresh UUID per call (rare; useful when the
    upstream uses the key to detect *attempt* identity rather than *request* identity).
    ``"none"`` disables key generation. The key is only sent if the provider opts
    in via ``BaseProvider.IDEMPOTENCY_HEADER_NAME``."""

    @classmethod
    def conservative(cls) -> RetryPolicy:
        """Fewer retries, longer backoffs. For pricey or non-idempotent operations
        (e.g. billed video generation) where the cost of a duplicate submit
        outweighs the cost of a full failure."""
        return cls(
            max_attempts=2,
            initial_backoff_sec=2.0,
            max_backoff_sec=60.0,
        )

    @classmethod
    def aggressive(cls) -> RetryPolicy:
        """More retries, shorter backoffs. For idempotent reads, cheap probes,
        and analysis pipelines where transient failures are common and the work
        is safe to retry."""
        return cls(
            max_attempts=7,
            initial_backoff_sec=0.5,
            max_backoff_sec=15.0,
        )

    @classmethod
    def disabled(cls) -> RetryPolicy:
        """No retries — fail fast on first error. For tests, debugging, or
        scenarios where the caller wants to handle retry logic externally."""
        return cls(
            max_attempts=1,
            retryable_codes=frozenset(),
        )

    def compute_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Delay (seconds) before retry attempt ``attempt`` (1-based).

        ``Retry-After`` wins over computed backoff when ``respect_retry_after``
        is set, except when the policy was constructed with ``jitter="none"``
        and the server hint is missing — then the computed value is fully
        deterministic for testability.
        """
        if retry_after is not None and self.respect_retry_after:
            return min(retry_after, MAX_RETRY_AFTER_SEC)
        base = min(
            self.initial_backoff_sec * (self.backoff_multiplier ** (attempt - 1)),
            self.max_backoff_sec,
        )
        if self.jitter == "full":
            return random.uniform(0, base)  # noqa: S311 — jitter, not crypto
        if self.jitter == "equal":
            return base / 2 + random.uniform(0, base / 2)  # noqa: S311
        return base

    def should_retry(self, error_code: ProviderErrorCode | None, attempt: int) -> bool:
        """Whether to attempt another retry given the failure code and prior attempts.

        ``attempt`` is the attempt number that just failed (1-based). The check
        is ``attempt < max_attempts`` because the next retry would be
        ``attempt + 1``, which must be ``≤ max_attempts``.
        """
        if attempt >= self.max_attempts:
            return False
        if error_code is None:
            return False
        return error_code in self.retryable_codes

    def make_idempotency_key(self, step: Step) -> str | None:
        """Derive the idempotency key value for this step, or ``None`` to skip.

        The key is only injected into the wire request when the provider has
        opted in by setting ``BaseProvider.IDEMPOTENCY_HEADER_NAME``. Returning
        the same value across retries of one step is what makes the upstream
        able to dedupe — that's why ``"step_id"`` (a UUID stable for the
        lifetime of the step) is the default.
        """
        if self.idempotency_key_strategy == "none":
            return None
        if self.idempotency_key_strategy == "step_id":
            return step.step_id
        if self.idempotency_key_strategy == "uuid_per_attempt":
            return str(uuid.uuid4())
        return None


def call_with_rate_limit_retry(fn: Callable[[], _T], *, policy: RetryPolicy | None = None) -> _T:
    """Run ``fn()``, retrying on rate-limited ``ProviderError`` per ``policy`` (#221).

    Backs the opt-in ``retry_on_rate_limit=`` flag on the standalone
    ``chat()``/``achat()`` convenience helpers (``genblaze_openai.chat``,
    ``genblaze_google.chat``). Those helpers already parse a server's
    ``Retry-After`` hint onto ``ProviderError.retry_after`` but historically
    raised immediately instead of waiting — this wraps the single API call
    they make and sleeps between attempts using the same backoff math as
    ``BaseProvider``'s pipeline retry loop (``RetryPolicy.compute_delay`` —
    server hint wins when present, else exponential backoff with jitter).

    Narrowed to ``ProviderErrorCode.RATE_LIMIT`` — this helper exists to fix
    429 backoff specifically, not to become a general retry wrapper for the
    convenience helpers — but still defers to ``policy.should_retry`` (which
    checks both ``policy.retryable_codes`` membership and the ``max_attempts``
    budget) rather than hardcoding the attempt-cap check. A caller who passes
    a ``policy`` with ``RATE_LIMIT`` excluded from ``retryable_codes`` (e.g.
    ``RetryPolicy.disabled()``) gets no retry, same as they would on the
    ``BaseProvider`` pipeline path. Any other exception (including other
    ``ProviderError`` codes) propagates on the first attempt.

    Bounded worst case: total wait is at most ``(max_attempts - 1) *
    MAX_RETRY_AFTER_SEC`` — with the default policy (6 attempts), up to ~10
    minutes if a misbehaving upstream returns the maximum ``Retry-After`` hint
    on every attempt. There's no wall-clock deadline gate (unlike
    ``BaseProvider``'s pipeline retry, which bails out once a step's overall
    ``timeout`` would be exceeded) — this is a single, unscheduled call with no
    equivalent deadline to check against. Pass a tighter ``policy`` (e.g.
    ``max_attempts=2``) if that worst case is unacceptable for your call site.

    Synchronous by design: callers on the async path (``achat``) already run
    the whole sync call — including this retry loop — via
    ``asyncio.to_thread``, so the blocking ``time.sleep`` here only occupies
    the worker thread, never the event loop.

    Args:
        fn: Zero-arg callable to invoke. Should raise ``ProviderError`` (with
            ``error_code``/``retry_after`` set) on failure.
        policy: Controls the attempt cap, retryable-code set, and backoff
            timing. Defaults to ``RetryPolicy()`` (6 attempts, ``Retry-After``
            honored, exponential + full-jitter fallback when no hint is
            present).

    Raises:
        ProviderError: Re-raised once attempts are exhausted, or immediately
            if the error isn't classified as ``RATE_LIMIT`` (or the policy
            doesn't consider ``RATE_LIMIT`` retryable).
    """
    policy = policy or RetryPolicy()
    attempt = 1
    while True:
        try:
            return fn()
        except ProviderError as exc:
            if exc.error_code != ProviderErrorCode.RATE_LIMIT or not policy.should_retry(
                exc.error_code, attempt
            ):
                raise
            delay = policy.compute_delay(attempt, retry_after=exc.retry_after)
            # Multi-minute sleeps are otherwise silent to the caller — log so a
            # stalled archive run is visible instead of looking hung.
            logger.info(
                "rate limited, retrying in %.1fs (attempt %d/%d)",
                delay,
                attempt,
                policy.max_attempts,
            )
            time.sleep(delay)
            attempt += 1


__all__ = [
    "MAX_RETRY_AFTER_SEC",
    "PRE_RESPONSE_EXCEPTIONS",
    "IdempotencyStrategy",
    "JitterStrategy",
    "RetryPolicy",
    "call_with_rate_limit_retry",
    "retry_after_from_response",
]
