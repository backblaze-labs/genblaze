<!-- last_verified: 2026-04-24 -->
# Retry Policy Unification

Unifies transient-failure handling across every provider connector in one place: `BaseProvider`.
Replaces four near-duplicate retry loops with a single `_retry_phase` helper, adds `Retry-After`
honoring and a wire-visible `StepRetriedEvent`, fixes `jittered_backoff` to AWS full-jitter, and
makes `submit()` retry safely on pre-response network errors.

## Problem

- `submit()` is not wrapped in any retry — a single 502 on the initial submit fails the whole
  step unless the caller sets `config.max_retries`, which defaults to `0`.
- `fetch_output()` is not wrapped in any retry — a transient 503 on the final GET drops all work
  even though the upstream job succeeded.
- Four near-identical retry loops exist in `base.py` (`_attempt_once`, `_attempt_once_async`,
  `resume`, `aresume`). Any policy change has to land four times.
- `jittered_backoff` returns `base * (1 + uniform(0, 0.25))` — always ≥ base, only ±12.5% effective
  spread. Not true full jitter; poor at decorrelating parallel clients.
- `Retry-After` on 429 / 503 is ignored — we sleep for our own backoff instead of the server's hint.
- Retries are log-only; no wire event, so UIs show silence between attempt 1 and attempt N.

## Non-goals (deliberately excluded)

- `RetryPolicy` dataclass with 8 knobs. A single `retry_attempts` class attribute covers today's
  needs. Evidence, then knobs.
- Per-phase retry policies. Phase differences are implementation details, not API surface.
- Circuit breaker. Wrong fit for one-shot generation pipelines.
- `Idempotency-Key` header plumbing. Per-connector work to add when upstream actually supports
  it; no provider we target documents support today.
- Wrapping `submit()` on post-response errors (5xx / ReadTimeout) without an idempotency key —
  could double-bill. Outer `invoke()` loop still handles these when caller opts in.

## Scope — file touch list

| File | Change |
|---|---|
| `libs/core/genblaze_core/_utils.py` | Replace `jittered_backoff` body with full-jitter `uniform(0, min(cap, 2**attempt))`. Signature unchanged. |
| `libs/core/genblaze_core/providers/retry.py` | **New.** `MAX_RETRY_AFTER_SEC` constant, `retry_after_from_response(resp)` parser (int seconds or HTTP-date, clamped), `PRE_RESPONSE_EXCEPTIONS` tuple (`httpx.ConnectError`, `httpx.ConnectTimeout`, `httpx.PoolTimeout`). |
| `libs/core/genblaze_core/exceptions.py` | Add `retry_after: float \| None = None` and `attempts: int = 1` kwargs to `ProviderError.__init__`. |
| `libs/core/genblaze_core/providers/base.py` | Add `retry_attempts: int = 5` class attribute (`poll_transient_retries` kept as deprecated alias). Add one `_retry_phase(fn, *, phase, step, config, timeout, start_time, retry_on=None)` helper (sync + async). Replace the 4 inline retry loops with calls into it. Wrap `submit` (phase="submit", `retry_on=PRE_RESPONSE_EXCEPTIONS`) and `fetch_output` (phase="fetch", all transient codes). |
| `libs/core/genblaze_core/observability/events.py` | Add `StepRetriedEvent` variant (`type="step.retried"`, `phase`, `attempt`, `delay_sec`, `error_code`, `error`). Extend `AnyStreamEvent` union and `StreamEventType` literal. |
| `libs/spec/schemas/events/v1/step-retried.schema.json` | **New.** Matches the Python model. |
| `libs/spec/schemas/events/v1/stream-event.schema.json` | Add `step.retried` to `oneOf` and `discriminator.mapping`. |
| `libs/core/tests/unit/test_spec_conformance.py` | Register `StepRetriedEvent` in the schema-parity table + roundtrip. |
| `libs/connectors/gmicloud/genblaze_gmicloud/_base.py` | Attach `retry_after=retry_after_from_response(resp)` to every `ProviderError` raised on HTTP 4xx/5xx (submit, poll, fetch). |
| `libs/connectors/gmicloud/genblaze_gmicloud/{audio,image,provider,chat}.py` | Same — wherever `resp.status_code >= 400` raises. |
| `libs/core/tests/unit/test_provider_retry.py` | Extend with: full-jitter distribution check, `Retry-After` honored, `StepRetriedEvent` emitted, submit pre-response retry succeeds, submit post-response does **not** retry (default), fetch retry succeeds. |

## Behavioral guarantees after landing

1. A transient `httpx.ConnectError` on submit retries up to `retry_attempts` times transparently.
2. A transient 5xx on poll or fetch retries up to `retry_attempts` times transparently.
3. A `Retry-After: N` header overrides computed backoff, clamped to `MAX_RETRY_AFTER_SEC` (120s).
4. Every retry emits a `StepRetriedEvent` on the pipeline stream and is logged at WARNING.
5. Non-retryable codes (`AUTH_FAILURE`, `INVALID_INPUT`, `CONTENT_POLICY`, `MODEL_ERROR`) never
   consume retry budget.
6. Global `config.timeout` is respected: no retry that would exceed it is attempted.
7. All existing tests in `test_provider_retry.py` continue to pass unchanged (shim via alias).

## Test plan

- `test_full_jitter_distribution` — 1000 samples in `[0, cap]`, mean near cap/2 (±10%).
- `test_submit_pre_response_error_retries` — `httpx.ConnectError` first 2 attempts, success 3rd.
- `test_submit_post_response_5xx_does_not_retry_by_default` — one attempt, step fails.
- `test_fetch_transient_retry_recovers` — fetch raises 503 twice, succeeds.
- `test_retry_after_header_honored` — ProviderError with `retry_after=3.0` sleeps 3s, not jittered.
- `test_retry_after_clamped` — `retry_after=9999.0` clamped to `MAX_RETRY_AFTER_SEC`.
- `test_step_retried_event_emitted` — stream a pipeline with a flaky poll; assert event count + fields.
- Existing `test_poll_transient_retry_*` tests pass unchanged.

## Risk

- **Low**: the change is a consolidation + safety widening. Existing retry paths are behavior-preserving.
- **Migration**: `poll_transient_retries` kept as alias on `BaseProvider` — no connector needs
  changes unless it wants per-provider overrides.

## Out of this plan (follow-ups)

- Apply `Retry-After` wiring to the other 10 connectors. Trivial but individual PRs.
- `Idempotency-Key` once a target provider documents support.
- Webhook notifier already benefits automatically from the full-jitter fix (shares
  `jittered_backoff`).
