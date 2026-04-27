<!-- last_verified: 2026-04-25 -->
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
- Webhook notifier already benefits automatically from the full-jitter fix (shares
  `jittered_backoff`).

---

## Phase 2 — `RetryPolicy` class + idempotency scaffolding (2026-04-25)

**Status:** in-progress · **Target release:** `genblaze-core 0.2.6` · **Shape:** A (additive) ·
**Feedback ref:** F-2026-04-25-10, F-2026-04-25-11

### Why this lifts the Phase-1 non-goal

Phase 1 explicitly deferred the `RetryPolicy` dataclass ("evidence, then knobs") and
`Idempotency-Key` plumbing ("no provider we target documents support today"). Two events
flipped both decisions:

1. **CHANGELOG [0.2.5] overstated reality.** The release notes claim `genblaze-core`
   "exposes a `RetryPolicy` the caller can override per-provider" — but the shipped
   `providers/retry.py` only exports utility functions and constants. There is no
   `RetryPolicy` class. Either ship the class or retract the claim. We ship the class.
2. **Sample-app builder evidence.** A second-batch feedback report (2026-04-25) hits a
   real overrides-needed scenario: cost-sensitive video pipelines want fewer retries with
   longer backoffs than poll-heavy audio analysis. The `poll_transient_retries` class
   attribute requires subclassing — not a viable user surface.
3. **OpenAI documents `Idempotency-Key` support.** The Phase-1 assumption ("no target
   provider documents it") is out of date. Idempotency-key rollout is now per-provider
   work, blocked only on the scaffolding we add here.

### Design

`RetryPolicy` is a frozen dataclass exposing the seven knobs callers actually need.
Defaults match the current `BaseProvider` behavior so adoption is opt-in and existing
subclasses (and `poll_transient_retries` overrides) keep working unchanged.

```python
# libs/core/genblaze_core/providers/retry.py
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    initial_backoff_sec: float = 1.0
    max_backoff_sec: float = 30.0
    backoff_multiplier: float = 2.0
    jitter: Literal["none", "full", "equal"] = "full"
    respect_retry_after: bool = True
    retryable_codes: frozenset[ProviderErrorCode] = field(
        default_factory=lambda: frozenset({TIMEOUT, RATE_LIMIT, SERVER_ERROR})
    )
    idempotency_key_strategy: Literal["none", "step_id", "uuid_per_attempt"] = "step_id"

    @classmethod
    def conservative(cls) -> RetryPolicy: ...   # 2 attempts, 2s base, 60s cap — for billed video gens
    @classmethod
    def aggressive(cls) -> RetryPolicy: ...     # 7 attempts, 0.5s base — for cheap idempotent reads
    @classmethod
    def disabled(cls) -> RetryPolicy: ...       # 1 attempt, no retries — for tests / debugging

    def compute_delay(self, attempt: int, retry_after: float | None = None) -> float: ...
    def should_retry(self, error_code: ProviderErrorCode | None, attempt: int) -> bool: ...
    def make_idempotency_key(self, step: Step) -> str | None: ...
```

### Wiring

| Site | Change |
|---|---|
| `BaseProvider.__init__` | Accept `retry_policy: RetryPolicy \| None = None`. Store as `self._retry_policy = retry_policy or self._default_retry_policy()`. |
| `BaseProvider._default_retry_policy()` | New classmethod. Returns a `RetryPolicy` whose `max_attempts = cls.poll_transient_retries + 1` so subclasses that override the class attr keep their tuning. |
| `BaseProvider._is_retryable` | Now an instance method consulting `self._retry_policy.should_retry(...)`. Static-method shim retained as `_is_retryable_default(...)` for callers that pre-construct (deprecated, slated for removal in 0.3.0). |
| `BaseProvider._retry_delay` | Instance method consulting `self._retry_policy.compute_delay(attempt, retry_after=...)`. |
| `BaseProvider._retry_phase` / `_aretry_phase` | `max_budget = self._retry_policy.max_attempts - 1` (one initial attempt + N-1 retries). |
| `BaseProvider._emit_retry` | Reports `max_attempts = self._retry_policy.max_attempts` instead of `poll_transient_retries + 1`. |
| `BaseProvider.IDEMPOTENCY_HEADER_NAME` | New `ClassVar[str \| None] = None`. Per-provider opt-in (e.g. OpenAI = `"Idempotency-Key"`). |
| `BaseProvider._inject_idempotency_header(headers, step)` | Adds the header **iff** the provider opted in **and** the policy has a non-none strategy. Same key reused across retries of one step. |

### Backwards compatibility

- `poll_transient_retries: int = 5` class attribute kept. Subclasses overriding it (none
  in-tree today, per `grep`, but possible downstream) get equivalent behavior because
  `_default_retry_policy()` reads the attr.
- `jittered_backoff()` in `_utils` kept (still used by the webhook notifier and
  `invoke()`'s outer loop). `RetryPolicy.compute_delay` is a superset.
- All existing tests in `test_provider_retry.py` pass unchanged — same behavior for any
  call that doesn't pass `retry_policy=`.

### Idempotency rollout (this phase ships scaffolding only)

This phase establishes the contract; per-provider header rollouts are individual PRs:

| Provider | Header | Rollout phase |
|---|---|---|
| OpenAI | `Idempotency-Key` | Phase 2 (demonstration; ships with this plan) |
| Stripe-style providers | `Idempotency-Key` | Per-provider follow-up |
| GMICloud | upstream support unconfirmed | Blocks on probe |
| Others | upstream support unconfirmed | Per-provider follow-up |

### File touch list

| File | Change |
|---|---|
| `libs/core/genblaze_core/providers/retry.py` | Add `RetryPolicy` dataclass + presets + behavior methods. Update `__all__`. |
| `libs/core/genblaze_core/providers/__init__.py` | Re-export `RetryPolicy`. |
| `libs/core/genblaze_core/providers/base.py` | Wire `retry_policy=` through `__init__`, `_retry_phase`, `_aretry_phase`, `_emit_retry`. Add `_default_retry_policy`, `_inject_idempotency_header`, `IDEMPOTENCY_HEADER_NAME`. |
| `libs/connectors/openai/genblaze_openai/_base.py` | Set `IDEMPOTENCY_HEADER_NAME = "Idempotency-Key"` (demonstration). |
| `libs/core/tests/unit/test_retry_policy.py` | **New.** Unit tests for `RetryPolicy` (compute_delay distribution, should_retry gates, make_idempotency_key strategies, presets). |
| `libs/core/tests/unit/test_provider_retry.py` | Add `test_provider_accepts_retry_policy_kwarg`, `test_retry_policy_overrides_max_attempts`, `test_idempotency_header_injected_when_opted_in`. |
| `libs/core/tests/conformance/test_provider_contract.py` | Add `test_accepts_retry_policy_kwarg` parametric across discovered providers. |
| `docs/features/retry-policy.md` | **New.** When to override, preset chooser, idempotency rollout status table. |
| `CHANGELOG.md` | Move the (overstated) [0.2.5] `RetryPolicy` line into a new `[Unreleased]` corrective entry that lists what actually ships now. |

### Acceptance gates

- [ ] `RetryPolicy()` constructable with no args; preset classmethods return distinct configs.
- [ ] `BaseProvider(retry_policy=RetryPolicy.conservative())` works for every connector
      (covered by parametric conformance test).
- [ ] Every existing `test_provider_retry.py` test passes unchanged.
- [ ] `make lint && make typecheck && make test` green.
- [ ] `make ts-types` idempotent (no schema change in this phase).
- [ ] Doc page `docs/features/retry-policy.md` published; CHANGELOG corrective entry merged.

### Out of Phase 2 (still follow-ups)

- Per-pipeline override via `Pipeline.run(retry_policy=...)` and `Pipeline.step(retry_policy=...)`.
  Scope creep — `RunnableConfig` already has `max_retries`; integrating cleanly needs a
  separate design for how policy composes across step / pipeline / provider.
- `Idempotency-Key` rollout to providers other than OpenAI. Each one is a small per-provider PR.
- Telemetry: `retry_policy_summary` field on `PipelineCompletedEvent` so observability
  backends can group by policy. Defer until a user asks.

