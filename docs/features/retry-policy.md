<!-- last_verified: 2026-06-17 -->
# Retry Policy

`RetryPolicy` is the user-tunable knob for how `BaseProvider` retries transient
failures during the `submit / poll / fetch_output` lifecycle. Pass one to a
provider's constructor; defaults reproduce the historical "5 retries beyond
the initial call" behavior so existing code keeps working unchanged.

```python
from genblaze_core.providers import RetryPolicy
from genblaze_openai import SoraProvider

provider = SoraProvider(retry_policy=RetryPolicy.conservative())
```

## When to override the default

| Scenario | Suggested policy | Why |
|---|---|---|
| Billed video generation (Sora, Runway, Veo) | `RetryPolicy.conservative()` | One duplicate submit costs more than a clean failure. Shorter budget, longer backoffs avoid bunching. |
| Cheap idempotent reads (chat, polls) | `RetryPolicy.aggressive()` | Transient hiccups are common; retries are free. |
| Tests / debugging | `RetryPolicy.disabled()` | Fail fast on first error so you see the real cause. |
| Batch/eval pipelines | Custom — usually `aggressive()` plus narrowed `retryable_codes` | Tune to your upstream's known failure modes. |

If you're not sure, leave the default. The default policy was chosen to match
the pre-class behavior of `BaseProvider.poll_transient_retries=5`.

## The seven knobs

| Field | Default | What it controls |
|---|---|---|
| `max_attempts` | `6` | Total attempts per phase including the initial. Set to `1` to disable retries. |
| `initial_backoff_sec` | `1.0` | Base delay before the first retry. |
| `max_backoff_sec` | `30.0` | Hard cap on computed delay (does not affect server-supplied `Retry-After`, which has its own 120 s cap). |
| `backoff_multiplier` | `2.0` | Exponential growth between successive attempts. |
| `jitter` | `"full"` | `"full"` (AWS-style `uniform(0, base)`), `"equal"` (`base/2 + uniform(0, base/2)`), or `"none"`. Full jitter is best at de-syncing thundering herds; `"none"` is for deterministic tests. |
| `respect_retry_after` | `True` | Honor server `Retry-After` headers (delta-seconds or HTTP-date). Clamped to 120 s. |
| `retryable_codes` | `{TIMEOUT, RATE_LIMIT, SERVER_ERROR}` | Normalized `ProviderErrorCode` values eligible for retry. Outside this set: fail fast. |
| `idempotency_key_strategy` | `"step_id"` | How `make_idempotency_key` derives the value sent on retry-eligible submits. See "Idempotency keys" below. |

## Three presets

```python
RetryPolicy.conservative()  # 2 attempts, 2 s base, 60 s cap
RetryPolicy.aggressive()    # 7 attempts, 0.5 s base, 15 s cap
RetryPolicy.disabled()      # 1 attempt, no retries (no codes retryable)
```

## How retries compose with other knobs

There are two retry layers. Most users only need to think about the first.

1. **Phase-level transient retries** (controlled by `RetryPolicy`) — wrap each
   of `submit / poll / fetch_output` so a single 5xx mid-poll doesn't fail a
   long-running video generation. The new policy controls **all of these**.
2. **Step-level retries** (`config["max_retries"]` passed to `Pipeline.run()`)
   — retry the step after a phase-level budget is exhausted. If the failed
   attempt already produced an upstream prediction ID from the current
   `submit()` call, the retry resumes that existing job instead of calling
   `submit()` again. This also covers a transient `on_submit` checkpoint
   failure after the upstream job was created: before an automatic resume polls
   or fetches, the base provider calls `on_submit(step_id, prediction_id)` again
   with the same prediction ID, so checkpoint callbacks must be idempotent for a
   given `(step_id, prediction_id)`. Caller-supplied
   `step.metadata["upstream_id"]` is observability data and is not trusted as
   retry authority. Only failures before a current upstream ID exists use a
   fresh submit. The retryable-codes set is now also taken from the policy, so
   tuning `RetryPolicy.retryable_codes` affects both layers consistently.

Submit retries have a special rule: only **pre-response** exception types
(httpx `ConnectError`, `ConnectTimeout`, `PoolTimeout`) are eligible by
default — replaying a request that may already have hit the server could
double-bill. Once a provider opts into idempotency-key injection, this
restriction is safe to widen via your own subclass.

Poll/fetch failures have the opposite safety rule: the upstream generation
already exists, so step-level retries re-run the base poll/fetch resume loop
against the same in-process prediction ID. This avoids duplicate renders and
duplicate charges when the transient error happened after generation started.
After a prediction ID is recorded for the current invoke, automatic retries stay
bound to that ID and do not fall back to a fresh submit, because a fresh submit
can double-bill. If the step-level retry budget is exhausted while resuming, the
provider logs that the resume budget was exhausted without re-submit and records
`genblaze.step_retry.resume_exhausted` span metadata.

Internal step retries use the protected `_resume_once()` / `_aresume_once()`
hooks, which are also used by public `resume()` / `aresume()`, and share the
same base poll/fetch helper. Provider authors normally customize shared resume
behavior by overriding `poll()` / `fetch_output()`. Public `resume()` progress
still emits `resumed`; automatic step retry resume emits `retry_resumed`.

Each step-level retry logs its route at INFO (`route=resume` or
`route=submit`) and records `genblaze.step_retry.route` /
`genblaze.step_retry.resumed` span attributes. The route log includes `step_id`
and `run_id` when available. Upstream prediction IDs are not included in retry
route logs to keep logs concise, but Genblaze treats prediction IDs as
observability identifiers rather than secrets. They remain available in
`step.metadata["upstream_id"]`, `ProgressEvent.request_id`, and stream
completion/failure event `request_id` fields for checkpointing and UI
correlation.

## Idempotency keys

When a provider sets `IDEMPOTENCY_HEADER_NAME` (e.g. `"Idempotency-Key"` for
OpenAI), `BaseProvider._inject_idempotency_header()` adds the header on every
submit, with the value derived from `policy.make_idempotency_key(step)`.

Three strategies:

- `"step_id"` *(default)* — reuses `step.step_id` (a UUID stable for the
  step's lifetime). Same value across retries, so the upstream can dedupe.
- `"uuid_per_attempt"` — fresh UUID per call. Useful when the upstream uses
  the key to identify the *attempt* rather than the *request*. Rare.
- `"none"` — disables key generation entirely.

### Per-provider rollout status

The scaffolding is in core; per-provider header opt-ins are individual PRs.

| Provider | `IDEMPOTENCY_HEADER_NAME` | Status |
|---|---|---|
| OpenAI | `"Idempotency-Key"` | scaffolding ready; per-provider PR pending |
| GMICloud | unconfirmed | blocked on live-API confirmation |
| Google | unconfirmed | provider-API spec doesn't document support |
| Runway | unconfirmed | provider-API spec doesn't document support |
| Luma | unconfirmed | provider-API spec doesn't document support |
| Decart | unconfirmed | provider-API spec doesn't document support |
| Replicate | unconfirmed | provider-API spec doesn't document support |
| ElevenLabs | unconfirmed | provider-API spec doesn't document support |
| Stability Audio | unconfirmed | provider-API spec doesn't document support |
| LMNT | unconfirmed | provider-API spec doesn't document support |
| NVIDIA NIM | unconfirmed | provider-API spec doesn't document support |

To enable for a provider you control, set the class attribute and ensure your
submit path passes the result of `_inject_idempotency_header(headers, step)`
to the underlying HTTP client.

## Examples

```python
from genblaze_core.providers import RetryPolicy
from genblaze_openai import SoraProvider

# Conservative for billed video gen.
sora = SoraProvider(retry_policy=RetryPolicy.conservative())

# Custom: only retry rate-limits, full jitter, max 4 attempts.
from genblaze_core.models.enums import ProviderErrorCode

custom = RetryPolicy(
    max_attempts=4,
    retryable_codes=frozenset({ProviderErrorCode.RATE_LIMIT}),
    jitter="full",
)

# Tests: disable retries entirely so failures surface immediately.
test_provider = SoraProvider(retry_policy=RetryPolicy.disabled())
```

## Migrating from `poll_transient_retries`

The historical class attribute `BaseProvider.poll_transient_retries: int = 5`
still works. When no `retry_policy=` is passed, the active policy is built
from `self.poll_transient_retries` at access time, so legacy code that does
`provider.poll_transient_retries = 2` after construction keeps working.

Recommended migration:

```python
# Before
class MyProvider(BaseProvider):
    poll_transient_retries = 2

# After
provider = MyProvider(retry_policy=RetryPolicy(max_attempts=3))
```

The new form is strictly more expressive (you also get to tune backoff,
jitter, and codes) and survives factory-style construction patterns where
mutating class attributes after the fact isn't practical.

## See also

- [`docs/features/observability.md`](observability.md) — `StepRetriedEvent`
  fires on every retry attempt, with `phase`, `attempt`, `delay_sec`,
  `error_code`, and `error` so UIs can render "Retrying… (2/3)".
- [`docs/exec-plans/active/retry-policy-unification.md`](../exec-plans/active/retry-policy-unification.md) —
  Phase 2 design notes, sequencing, and follow-ups (per-pipeline override,
  provider idempotency rollout).
