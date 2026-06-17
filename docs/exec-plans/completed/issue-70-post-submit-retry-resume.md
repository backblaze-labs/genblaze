<!-- last_verified: 2026-06-17 -->
# Issue 70 Post-Submit Retry Resume

Fixes issue #70: step-level retries after a successful upstream submit must resume
the existing prediction instead of submitting another billed generation.

## Scope

| File | Change |
|---|---|
| `libs/core/genblaze_core/providers/base.py` | Route step-level retries through the resume lifecycle when a failed prior attempt reached poll/fetch with an `upstream_id`. Keep pre-submit and submit failures on the existing full re-submit path. |
| `libs/core/tests/unit/test_provider_retry.py` | Add regression coverage for fetch retry, poll retry, submit retry, and async fetch retry call counts. |
| `docs/features/retry-policy.md` | Document that post-submit step retries resume the existing upstream job. |

## Acceptance Criteria

- `fetch_output`-phase retries do not call `submit()` again.
- Poll/fetch-phase failures resume the existing prediction.
- Pre-submit/submit failures continue to re-submit.
- Sync and async provider paths behave consistently.

## Test Plan

- `cd libs/core && pytest tests/unit/test_provider_retry.py -v`
- `make lint`
- `make test`
