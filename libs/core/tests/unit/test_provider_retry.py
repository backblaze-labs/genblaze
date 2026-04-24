"""Tests for BaseProvider retry logic and error classification."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

from genblaze_core.models.enums import ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import (
    BaseProvider,
    _adaptive_poll_interval,
    classify_api_error,
)
from genblaze_core.runnable.config import RunnableConfig


class _RetryProvider(BaseProvider):
    """Provider that fails N times then succeeds."""

    name = "retry-test"

    def __init__(self, fail_count: int = 0, error_msg: str = "server error 500"):
        super().__init__()
        self.fail_count = fail_count
        self._error_msg = error_msg
        self.attempts = 0

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.attempts += 1
        if self.attempts <= self.fail_count:
            raise RuntimeError(self._error_msg)
        return "pred-ok"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        return step


def _make_step() -> Step:
    return Step(provider="retry-test", model="test-model", prompt="hello")


class _ResumableProvider(BaseProvider):
    """Provider that tracks poll calls for resume testing."""

    name = "resumable"

    def __init__(self, polls_until_done: int = 2):
        super().__init__()
        self._polls_until_done = polls_until_done
        self._poll_count = 0
        self.submit_called = False

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.submit_called = True
        return "pred-resume"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        self._poll_count += 1
        return self._poll_count >= self._polls_until_done

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        from genblaze_core.models.asset import Asset

        step.assets.append(Asset(url="https://example.com/resumed.png", media_type="image/png"))
        return step


@patch("genblaze_core.providers.base.time.sleep")
def test_resume_skips_submit(mock_sleep) -> None:
    """resume() polls and fetches without calling submit()."""
    provider = _ResumableProvider(polls_until_done=2)
    step = _make_step()
    result = provider.resume("pred-resume", step, {"timeout": 60})

    assert result.status == StepStatus.SUCCEEDED
    assert not provider.submit_called
    assert len(result.assets) == 1


@patch("genblaze_core.providers.base.time.sleep")
def test_resume_timeout(mock_sleep) -> None:
    """resume() records timeout as a failed step (no longer raises)."""
    provider = _ResumableProvider(polls_until_done=999)
    step = _make_step()
    result = provider.resume("pred-resume", step, {"timeout": 0})
    assert result.status == StepStatus.FAILED
    assert "Resume poll timeout" in result.error


def test_no_retry_by_default() -> None:
    """Without max_retries, a failure is terminal."""
    provider = _RetryProvider(fail_count=1)
    step = _make_step()
    result = provider.invoke(step)
    assert result.status == StepStatus.FAILED
    assert provider.attempts == 1


@patch("genblaze_core.providers.base.time.sleep")
def test_retry_succeeds_after_transient_failure(mock_sleep) -> None:
    """Retry recovers from a transient server error."""
    provider = _RetryProvider(fail_count=2)
    step = _make_step()
    result = provider.invoke(step, {"max_retries": 3})
    assert result.status == StepStatus.SUCCEEDED
    assert result.retries == 2
    assert provider.attempts == 3
    # Backoff: 2^0=1s, 2^1=2s
    assert mock_sleep.call_count == 2


@patch("genblaze_core.providers.base.time.sleep")
def test_retry_exhausted(mock_sleep) -> None:
    """All retries exhausted -> step fails."""
    provider = _RetryProvider(fail_count=5)
    step = _make_step()
    result = provider.invoke(step, {"max_retries": 2})
    assert result.status == StepStatus.FAILED
    assert result.retries == 2
    assert provider.attempts == 3


def test_non_retryable_error_not_retried() -> None:
    """Auth errors are not retried even with max_retries set."""
    provider = _RetryProvider(fail_count=3, error_msg="401 unauthorized")
    step = _make_step()
    result = provider.invoke(step, {"max_retries": 3})
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.AUTH_FAILURE
    assert provider.attempts == 1
    assert result.retries == 0


@patch("genblaze_core.providers.base.time.sleep")
def test_retry_backoff_capped_at_30(mock_sleep) -> None:
    """Backoff base is capped at 30 seconds (plus up to 25% jitter)."""
    provider = _RetryProvider(fail_count=6)
    _make_step()
    provider.invoke(_make_step(), {"max_retries": 6, "timeout": 600})
    calls = [c.args[0] for c in mock_sleep.call_args_list]
    # Max jitter: 30 * 1.25 = 37.5
    assert all(c <= 37.5 for c in calls)
    # Should still be > 0 (jitter adds at least base * 1.0)
    assert all(c >= 1.0 for c in calls)


@patch("genblaze_core.providers.base.time.sleep")
def test_rate_limit_is_retryable(mock_sleep) -> None:
    """Rate limit errors should be retried."""
    provider = _RetryProvider(fail_count=1, error_msg="rate_limit exceeded 429")
    step = _make_step()
    result = provider.invoke(step, {"max_retries": 2})
    assert result.status == StepStatus.SUCCEEDED
    assert result.retries == 1


@patch("genblaze_core.providers.base.time.sleep")
def test_timeout_is_retryable(mock_sleep) -> None:
    """Timeout errors should be retried."""
    provider = _RetryProvider(fail_count=1, error_msg="request timed out")
    step = _make_step()
    result = provider.invoke(step, {"max_retries": 2})
    assert result.status == StepStatus.SUCCEEDED
    assert result.retries == 1


# --- Resume error handling tests ---


class _FailingPollProvider(BaseProvider):
    """Provider whose poll() raises an exception."""

    name = "failing-poll"

    def __init__(self):
        super().__init__()
        self.submit_called = False

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.submit_called = True
        return "pred-123"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        raise RuntimeError("network connection lost")

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        return step


class _FailingFetchProvider(BaseProvider):
    """Provider whose fetch_output() raises an exception."""

    name = "failing-fetch"

    def __init__(self):
        super().__init__()

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        return "pred-123"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        raise RuntimeError("server error 500 while fetching")


def test_resume_poll_error_marks_step_failed() -> None:
    """resume() should mark step as FAILED when poll() raises, not crash."""
    provider = _FailingPollProvider()
    step = _make_step()
    result = provider.resume("pred-123", step, {"timeout": 60})

    assert result.status == StepStatus.FAILED
    assert "network connection lost" in result.error
    assert result.error_code is not None
    assert result.completed_at is not None


def test_resume_fetch_error_marks_step_failed() -> None:
    """resume() should mark step as FAILED when fetch_output() raises."""
    provider = _FailingFetchProvider()
    step = _make_step()
    result = provider.resume("pred-123", step, {"timeout": 60})

    assert result.status == StepStatus.FAILED
    assert "server error 500" in result.error
    assert result.error_code == ProviderErrorCode.SERVER_ERROR
    assert result.completed_at is not None


@patch("genblaze_core.providers.base.time.sleep")
def test_resume_timeout_marks_step_failed(mock_sleep) -> None:
    """resume() timeout is caught and recorded on the step (not raised)."""
    provider = _ResumableProvider(polls_until_done=999)
    step = _make_step()
    result = provider.resume("pred-resume", step, {"timeout": 0})

    assert result.status == StepStatus.FAILED
    assert "Resume poll timeout" in result.error
    assert result.completed_at is not None


def test_aresume_poll_error_marks_step_failed() -> None:
    """aresume() should mark step as FAILED when poll() raises."""
    provider = _FailingPollProvider()
    step = _make_step()
    result = asyncio.run(provider.aresume("pred-123", step, {"timeout": 60}))

    assert result.status == StepStatus.FAILED
    assert "network connection lost" in result.error
    assert result.completed_at is not None


def test_aresume_fetch_error_marks_step_failed() -> None:
    """aresume() should mark step as FAILED when fetch_output() raises."""
    provider = _FailingFetchProvider()
    step = _make_step()
    result = asyncio.run(provider.aresume("pred-123", step, {"timeout": 60}))

    assert result.status == StepStatus.FAILED
    assert "server error 500" in result.error
    assert result.error_code == ProviderErrorCode.SERVER_ERROR


# --- Error classification tests ---


class TestClassifyError:
    """Test all branches of classify_api_error()."""

    def test_timeout(self):
        assert classify_api_error(RuntimeError("request timed out")) == ProviderErrorCode.TIMEOUT

    def test_timeout_keyword(self):
        assert classify_api_error(RuntimeError("timeout after 30s")) == ProviderErrorCode.TIMEOUT

    def test_rate_limit(self):
        err = classify_api_error(RuntimeError("rate_limit exceeded"))
        assert err == ProviderErrorCode.RATE_LIMIT

    def test_rate_limit_429(self):
        assert classify_api_error(RuntimeError("HTTP 429")) == ProviderErrorCode.RATE_LIMIT

    def test_auth_unauthorized(self):
        err = classify_api_error(RuntimeError("401 unauthorized"))
        assert err == ProviderErrorCode.AUTH_FAILURE

    def test_auth_forbidden(self):
        err = classify_api_error(RuntimeError("403 forbidden"))
        assert err == ProviderErrorCode.AUTH_FAILURE

    def test_invalid_input_400(self):
        err = classify_api_error(RuntimeError("400 bad request invalid"))
        assert err == ProviderErrorCode.INVALID_INPUT

    def test_invalid_validation(self):
        err = classify_api_error(RuntimeError("validation error"))
        assert err == ProviderErrorCode.INVALID_INPUT

    def test_model_error(self):
        err = classify_api_error(RuntimeError("model not found"))
        assert err == ProviderErrorCode.MODEL_ERROR

    def test_server_error_500(self):
        err = classify_api_error(RuntimeError("server 500"))
        assert err == ProviderErrorCode.SERVER_ERROR

    def test_server_error_502(self):
        err = classify_api_error(RuntimeError("bad gateway 502"))
        assert err == ProviderErrorCode.SERVER_ERROR

    def test_server_error_503(self):
        err = classify_api_error(RuntimeError("service unavailable 503"))
        assert err == ProviderErrorCode.SERVER_ERROR

    def test_unknown_fallback(self):
        err = classify_api_error(RuntimeError("something weird happened"))
        assert err == ProviderErrorCode.UNKNOWN


# --- Intra-poll transient-retry budget tests ---


class _FlakyPollProvider(BaseProvider):
    """poll() raises a retryable error N times then returns True."""

    name = "flaky-poll"

    def __init__(self, *, fail_count: int, error_msg: str = "server 503 temporary"):
        super().__init__()
        self._fail_count = fail_count
        self._error_msg = error_msg
        self.poll_calls = 0

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        return "pred-flaky"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        self.poll_calls += 1
        if self.poll_calls <= self._fail_count:
            raise RuntimeError(self._error_msg)
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        return step


@patch("genblaze_core.providers.base.time.sleep")
def test_poll_transient_retry_recovers(mock_sleep) -> None:
    """A few transient 5xx during poll() shouldn't fail the step."""
    provider = _FlakyPollProvider(fail_count=3)
    result = provider.invoke(_make_step(), {"timeout": 60})
    assert result.status == StepStatus.SUCCEEDED
    assert provider.poll_calls == 4  # 3 failures + 1 success


@patch("genblaze_core.providers.base.time.sleep")
def test_poll_transient_retry_budget_exhausted(mock_sleep) -> None:
    """After poll_transient_retries consecutive failures, escalate."""
    provider = _FlakyPollProvider(fail_count=99)
    provider.poll_transient_retries = 2
    result = provider.invoke(_make_step(), {"timeout": 60, "max_retries": 0})
    assert result.status == StepStatus.FAILED
    # 1 initial + 2 retries = 3 calls before giving up
    assert provider.poll_calls == 3


@patch("genblaze_core.providers.base.time.sleep")
def test_poll_non_retryable_not_retried(mock_sleep) -> None:
    """Auth errors during poll() don't consume the transient budget."""
    provider = _FlakyPollProvider(fail_count=99, error_msg="401 unauthorized")
    result = provider.invoke(_make_step(), {"timeout": 60, "max_retries": 0})
    assert result.status == StepStatus.FAILED
    assert provider.poll_calls == 1  # no retries


@patch("genblaze_core.providers.base.asyncio.sleep", return_value=None)
def test_apoll_transient_retry_recovers(mock_sleep) -> None:
    """Async path tolerates the same transient errors."""
    provider = _FlakyPollProvider(fail_count=2)
    result = asyncio.run(provider.ainvoke(_make_step(), {"timeout": 60}))
    assert result.status == StepStatus.SUCCEEDED
    assert provider.poll_calls == 3


# --- Poll cache thread-safety regression ---


def test_cleanup_poll_cache_snapshot_safe() -> None:
    """_cleanup_poll_cache must not raise if dict mutates during iteration."""
    provider = _RetryProvider(fail_count=0)
    # Populate stale entries so cleanup has work to do. Use a timestamp
    # older than max_age relative to monotonic() — on fresh CI runners
    # monotonic() can be under the 3600s TTL, so a literal 0.0 isn't stale.
    stale_ts = time.monotonic() - provider._poll_cache_max_age - 1
    for i in range(50):
        provider._poll_cache[f"k{i}"] = "v"
        provider._poll_cache_times[f"k{i}"] = stale_ts
    # Should drain all without "dictionary changed size during iteration"
    provider._cleanup_poll_cache()
    assert provider._poll_cache == {}
    assert provider._poll_cache_times == {}


class TestAdaptivePollInterval:
    """Test the adaptive poll interval backoff."""

    def test_starts_at_base(self):
        assert _adaptive_poll_interval(0, base=1.0) == 1.0

    def test_doubles_after_30s(self):
        assert _adaptive_poll_interval(30, base=1.0) == 2.0

    def test_doubles_again_after_60s(self):
        assert _adaptive_poll_interval(60, base=1.0) == 4.0

    def test_capped_at_max(self):
        assert _adaptive_poll_interval(300, base=1.0, max_interval=30.0) == 30.0

    def test_custom_max_interval(self):
        assert _adaptive_poll_interval(300, base=1.0, max_interval=10.0) == 10.0
