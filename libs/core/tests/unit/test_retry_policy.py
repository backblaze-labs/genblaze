"""Unit tests for ``RetryPolicy`` (data class + behavior methods).

Covers compute_delay distribution, should_retry gating, idempotency-key
strategies, and preset classmethods. Wiring tests (provider accepts kwarg,
overrides poll_transient_retries, idempotency header injected) live in
test_provider_retry.py to keep concerns separated.
"""

from __future__ import annotations

import statistics
from typing import Any

import pytest

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.retry import MAX_RETRY_AFTER_SEC, RetryPolicy


def _make_step() -> Step:
    """Minimal step for idempotency-key tests."""
    return Step(provider="test", model="m", prompt="p")


# --- Defaults ----------------------------------------------------------------


def test_default_policy_matches_pre_policy_behavior() -> None:
    """RetryPolicy() defaults reproduce the pre-class BaseProvider knobs."""
    p = RetryPolicy()
    assert p.max_attempts == 6  # 1 initial + 5 retries — same as poll_transient_retries=5
    assert p.initial_backoff_sec == 1.0
    assert p.max_backoff_sec == 30.0
    assert p.backoff_multiplier == 2.0
    assert p.jitter == "full"
    assert p.respect_retry_after is True
    assert p.idempotency_key_strategy == "step_id"
    assert ProviderErrorCode.TIMEOUT in p.retryable_codes
    assert ProviderErrorCode.RATE_LIMIT in p.retryable_codes
    assert ProviderErrorCode.SERVER_ERROR in p.retryable_codes
    # Deterministic codes are excluded by default.
    assert ProviderErrorCode.AUTH_FAILURE not in p.retryable_codes
    assert ProviderErrorCode.CONTENT_POLICY not in p.retryable_codes
    assert ProviderErrorCode.INVALID_INPUT not in p.retryable_codes
    assert ProviderErrorCode.MODEL_ERROR not in p.retryable_codes


def test_policy_is_frozen() -> None:
    """Frozen dataclass — mutating after construction raises."""
    p = RetryPolicy()
    with pytest.raises(Exception):  # FrozenInstanceError, but the type is implementation-detail
        p.max_attempts = 99  # type: ignore[misc]


def test_policy_is_hashable() -> None:
    """Frozen dataclass is hashable — usable as dict key / set member."""
    a = RetryPolicy()
    b = RetryPolicy()
    assert hash(a) == hash(b)
    assert {a, b} == {a}


# --- Presets -----------------------------------------------------------------


def test_conservative_preset_fewer_retries_longer_backoffs() -> None:
    p = RetryPolicy.conservative()
    assert p.max_attempts == 2
    assert p.initial_backoff_sec == 2.0
    assert p.max_backoff_sec == 60.0


def test_aggressive_preset_more_retries_shorter_backoffs() -> None:
    p = RetryPolicy.aggressive()
    assert p.max_attempts == 7
    assert p.initial_backoff_sec == 0.5
    assert p.max_backoff_sec == 15.0


def test_disabled_preset_no_retries_no_codes() -> None:
    """``disabled()`` truly disables — neither budget nor code gates allow retry."""
    p = RetryPolicy.disabled()
    assert p.max_attempts == 1
    assert p.retryable_codes == frozenset()
    # Budget gate fires first.
    assert p.should_retry(ProviderErrorCode.SERVER_ERROR, attempt=1) is False
    # Code gate fires when budget would have allowed.
    no_codes = RetryPolicy(max_attempts=10, retryable_codes=frozenset())
    assert no_codes.should_retry(ProviderErrorCode.SERVER_ERROR, attempt=1) is False


# --- compute_delay -----------------------------------------------------------


def test_compute_delay_no_jitter_is_deterministic() -> None:
    p = RetryPolicy(
        initial_backoff_sec=1.0, backoff_multiplier=2.0, max_backoff_sec=30.0, jitter="none"
    )
    assert p.compute_delay(attempt=1) == 1.0
    assert p.compute_delay(attempt=2) == 2.0
    assert p.compute_delay(attempt=3) == 4.0
    assert p.compute_delay(attempt=4) == 8.0
    assert p.compute_delay(attempt=5) == 16.0
    # Cap engages.
    assert p.compute_delay(attempt=6) == 30.0
    assert p.compute_delay(attempt=10) == 30.0


def test_compute_delay_full_jitter_distribution() -> None:
    """Full jitter samples uniformly from [0, base) — mean ≈ base/2 over many draws."""
    p = RetryPolicy(
        initial_backoff_sec=8.0, backoff_multiplier=2.0, max_backoff_sec=30.0, jitter="full"
    )
    samples = [p.compute_delay(attempt=1) for _ in range(2000)]
    # Range bound: every sample must be in [0, base].
    assert all(0.0 <= s <= 8.0 for s in samples)
    # Mean should land near base/2 (4.0); generous tolerance for 2000 samples.
    mean = statistics.fmean(samples)
    assert 3.5 < mean < 4.5


def test_compute_delay_equal_jitter_lower_bound_is_half_base() -> None:
    """Equal jitter samples from [base/2, base] — never less than half."""
    p = RetryPolicy(initial_backoff_sec=4.0, backoff_multiplier=2.0, jitter="equal")
    samples = [p.compute_delay(attempt=1) for _ in range(500)]
    assert all(2.0 <= s <= 4.0 for s in samples)


def test_compute_delay_retry_after_wins_when_respected() -> None:
    """Server hint overrides computed backoff."""
    p = RetryPolicy(initial_backoff_sec=1.0, jitter="none")
    assert p.compute_delay(attempt=5, retry_after=3.5) == 3.5


def test_compute_delay_retry_after_clamped() -> None:
    """Hostile or misconfigured server can't freeze the pipeline."""
    p = RetryPolicy()
    assert p.compute_delay(attempt=1, retry_after=99999.0) == MAX_RETRY_AFTER_SEC


def test_compute_delay_retry_after_ignored_when_disabled() -> None:
    """``respect_retry_after=False`` falls back to computed backoff."""
    p = RetryPolicy(initial_backoff_sec=1.0, jitter="none", respect_retry_after=False)
    assert p.compute_delay(attempt=1, retry_after=99.0) == 1.0


# --- should_retry ------------------------------------------------------------


def test_should_retry_within_budget_for_retryable_code() -> None:
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(ProviderErrorCode.SERVER_ERROR, attempt=1) is True
    assert p.should_retry(ProviderErrorCode.RATE_LIMIT, attempt=2) is True


def test_should_retry_blocked_at_budget() -> None:
    """attempt >= max_attempts returns False (next retry would exceed)."""
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(ProviderErrorCode.SERVER_ERROR, attempt=3) is False
    assert p.should_retry(ProviderErrorCode.SERVER_ERROR, attempt=99) is False


def test_should_retry_blocked_for_deterministic_codes() -> None:
    p = RetryPolicy(max_attempts=99)
    assert p.should_retry(ProviderErrorCode.AUTH_FAILURE, attempt=1) is False
    assert p.should_retry(ProviderErrorCode.CONTENT_POLICY, attempt=1) is False
    assert p.should_retry(ProviderErrorCode.INVALID_INPUT, attempt=1) is False
    assert p.should_retry(ProviderErrorCode.MODEL_ERROR, attempt=1) is False


def test_should_retry_blocked_for_none_code() -> None:
    """``None`` (unclassified) is conservatively non-retryable."""
    p = RetryPolicy(max_attempts=99)
    assert p.should_retry(None, attempt=1) is False


def test_should_retry_custom_codes() -> None:
    """Caller can narrow or widen the retryable set."""
    only_rate_limit = RetryPolicy(retryable_codes=frozenset({ProviderErrorCode.RATE_LIMIT}))
    assert only_rate_limit.should_retry(ProviderErrorCode.RATE_LIMIT, attempt=1) is True
    assert only_rate_limit.should_retry(ProviderErrorCode.SERVER_ERROR, attempt=1) is False


# --- make_idempotency_key ----------------------------------------------------


def test_make_idempotency_key_step_id_strategy_returns_stable_value() -> None:
    """``step_id`` strategy reuses ``step.step_id`` — stable across retries."""
    p = RetryPolicy(idempotency_key_strategy="step_id")
    step = _make_step()
    assert p.make_idempotency_key(step) == step.step_id
    assert p.make_idempotency_key(step) == step.step_id  # second call: same value


def test_make_idempotency_key_uuid_per_attempt_returns_fresh_value() -> None:
    """``uuid_per_attempt`` strategy generates a new UUID each call."""
    p = RetryPolicy(idempotency_key_strategy="uuid_per_attempt")
    step = _make_step()
    a = p.make_idempotency_key(step)
    b = p.make_idempotency_key(step)
    assert a is not None and b is not None
    assert a != b
    # Both are valid UUID strings.
    import uuid

    uuid.UUID(a)
    uuid.UUID(b)


def test_make_idempotency_key_none_strategy_returns_none() -> None:
    """``none`` disables key generation."""
    p = RetryPolicy(idempotency_key_strategy="none")
    assert p.make_idempotency_key(_make_step()) is None


# --- Equality / repr ---------------------------------------------------------


def test_policy_equality_by_value() -> None:
    """Frozen dataclass — value equality."""
    a = RetryPolicy(max_attempts=3, jitter="none")
    b = RetryPolicy(max_attempts=3, jitter="none")
    assert a == b


def test_policy_repr_contains_diagnostic_fields() -> None:
    """repr surfaces tunables for log diagnostics."""
    p = RetryPolicy(max_attempts=4)
    r = repr(p)
    assert "max_attempts=4" in r
    assert "jitter=" in r


# --- Type hints --------------------------------------------------------------


def test_policy_accepts_typed_arguments() -> None:
    """Sanity: literal-typed fields accept their declared values without error."""
    # Should not raise — exercises the Literal types.
    RetryPolicy(jitter="none")
    RetryPolicy(jitter="full")
    RetryPolicy(jitter="equal")
    RetryPolicy(idempotency_key_strategy="step_id")
    RetryPolicy(idempotency_key_strategy="uuid_per_attempt")
    RetryPolicy(idempotency_key_strategy="none")


# --- Edge cases --------------------------------------------------------------


def test_compute_delay_attempt_zero_behaves() -> None:
    """attempt=0 is unusual but shouldn't blow up — base ** -1 = 0.5."""
    p = RetryPolicy(initial_backoff_sec=1.0, backoff_multiplier=2.0, jitter="none")
    # 1.0 * 2 ** (0 - 1) = 0.5
    assert p.compute_delay(attempt=0) == pytest.approx(0.5)


def test_max_backoff_caps_jittered_value() -> None:
    """Large attempt numbers still cap at max_backoff (jitter ranges from 0..cap)."""
    p = RetryPolicy(
        initial_backoff_sec=1.0, backoff_multiplier=2.0, max_backoff_sec=10.0, jitter="full"
    )
    samples = [p.compute_delay(attempt=20) for _ in range(100)]
    # All samples must be in [0, max_backoff] — never exceed cap.
    assert all(0.0 <= s <= 10.0 for s in samples)


# --- Wiring smoke test (constructor accepts the policy) ---------------------


def test_base_provider_accepts_retry_policy_kwarg() -> None:
    """Smoke test that BaseProvider's constructor accepts retry_policy=."""
    from genblaze_core.providers.base import BaseProvider

    class _StubProvider(BaseProvider):
        name = "stub"

        def submit(self, step: Step, config: Any = None) -> Any:
            return "p"

        def poll(self, prediction_id: Any, config: Any = None) -> bool:
            return True

        def fetch_output(self, prediction_id: Any, step: Step) -> Step:
            return step

    custom = RetryPolicy.conservative()
    p = _StubProvider(retry_policy=custom)
    assert p.retry_policy is custom


def test_base_provider_falls_back_to_poll_transient_retries() -> None:
    """When no retry_policy=, the property reads instance poll_transient_retries
    at access time (so legacy ``provider.poll_transient_retries = 2`` still works)."""
    from genblaze_core.providers.base import BaseProvider

    class _StubProvider(BaseProvider):
        name = "stub"

        def submit(self, step: Step, config: Any = None) -> Any:
            return "p"

        def poll(self, prediction_id: Any, config: Any = None) -> bool:
            return True

        def fetch_output(self, prediction_id: Any, step: Step) -> Step:
            return step

    p = _StubProvider()
    # Default class attr: poll_transient_retries=5 → max_attempts=6.
    assert p.retry_policy.max_attempts == 6
    # Mutate instance — property re-reads on next access.
    p.poll_transient_retries = 2
    assert p.retry_policy.max_attempts == 3
