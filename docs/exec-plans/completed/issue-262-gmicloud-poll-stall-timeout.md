<!-- last_verified: 2026-09-03 -->
# Issue 262: Seedance 2.0 requests in agent pipeline intermittently get infinitely stalled while returning "dispatched" status

> **Status: IMPLEMENTED.** Branch `issue/262-gmicloud-poll-stall-timeout` (commit `6ee33b3`). gmicloud suite green (265 passed, 8 pre-existing skips); full-repo `make test` not yet re-run cleanly on this branch.

## Problem

GitHub issue: [#262](https://github.com/backblaze-labs/genblaze/issues/262)

A user integrating genblaze + gmiCloud into [Samsar](https://github.com/samsarone/samsar) runs multi-layer text→video jobs (Seedance 2.0). In a 10-layer fan-out, ~7 layers finish and ~3 hang indefinitely (60+ minutes) in a non-terminal state — Samsar labels this "dispatched"; on the genblaze side the step is stuck in `StepStatus.PROCESSING`. A hung step can neither be retried nor failed, so the agent stalls. Expected behaviour (from the issue): **every request must reach a terminal state — complete or fail — so the caller can handle or retry it.**

The video path runs through `GMICloudVideoProvider`, which inherits the shared poll loop in `GMICloudBase` (`libs/connectors/gmicloud/genblaze_gmicloud/_base.py`). The per-job wait loop lives in core: `_poll_fetch_once` (`libs/core/genblaze_core/providers/base.py:1443`) and its async twin `_apoll_fetch_once` (:1485), which drive `self.poll()` until it returns `True`.

GMICloud's documented request-queue statuses are exactly `queued`, `processing`, `success`, `failed`, `cancelled` (verified against `docs.gmicloud.ai`). The connector's terminal set is therefore **correct**:

```python
_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})   # _base.py:33
```

So this is **not** a status-vocabulary mismatch. The real defect: under parallel load some Seedance jobs genuinely wedge upstream in `queued`/`processing` and never transition. `poll()` (`_base.py:267-290`) faithfully returns `False` for every non-terminal status, forever. The **only** backstop is the core loop's wall-clock timeout:

- `timeout` is taken verbatim from the caller's config: `timeout = (config or {}).get("timeout", DEFAULT_TIMEOUT)` (`base.py:1712/1758/1769/1955`), `DEFAULT_TIMEOUT = 600.0` (`base.py:60`). There is **no upper clamp** and **no connector-enforced ceiling**.
- Long video needs a large per-step `timeout` (the connector README examples use `.run(timeout=600)`); Samsar sets it large enough that the 60s+ Seedance jobs never trip it, so a wedged job outlives any practical wall clock.
- When the timeout *does* fire, `invoke()` does not raise — it returns a `FAILED` step. The core raises a bare `"Poll timeout after Ns"` `ProviderError` (`base.py:1467`, `:1509`) with no explicit `error_code`, but the invoke handler classifies it to `TIMEOUT` via string-match (`"timeout" in msg`, `base.py:145`). So the step correctly ends `FAILED` / `error_code=timeout` — the message is just opaque (no request id / upstream status). The defect is purely that this only happens when the caller set a *bounded* timeout.

### Reproduced locally (deterministic, no key)

Stub the HTTP client so poll always returns `{"status": "processing"}` and drive the real `provider.invoke()`:

- `timeout=12` → step ends `FAILED`, `error_code=ProviderErrorCode.TIMEOUT`, `"Poll timeout after 12.0s"` — the safety net firing.
- `timeout=3600` (the long-video case) → the loop never terminates; it polls forever (after the ~24s `estimated_seconds` initial delay). **This is #262.**

## Fix

Add a **connector-enforced maximum polling ceiling** to `GMICloudBase` so a wedged upstream request always becomes a terminal, retryable failure within a bounded time, independent of the caller-supplied `config["timeout"]`. Keep the change inside the gmicloud connector (no core edits) to keep the blast radius minimal per CLAUDE.md.

1. **`GMICloudBase.__init__`** (`_base.py`): add `max_poll_seconds: float | None = 1800.0` (30 min — generous for long video, still bounded; `None` disables and falls back to the core timeout only). Store it, and add a per-instance `_poll_first_seen: dict[str, float]` guarded by a `threading.Lock`, mirroring the existing thread-safe `_poll_cache` / `_poll_cache_lock` pattern in core (`base.py:541-581`) — required because sync fan-out shares one provider across a `ThreadPoolExecutor` and async fan-out calls `poll()` via `asyncio.to_thread` (`base.py:1498`).

2. **`GMICloudBase.poll()`** (`_base.py:267`): on each call, record first-seen monotonic time for `prediction_id` (once), then after the normal terminal check:
   - If status is terminal → clear the first-seen entry and return `True` (unchanged behaviour).
   - If status is non-terminal **and** `max_poll_seconds` is set and elapsed-since-first-seen exceeds it → raise
     `ProviderError(f"GMICloud request {prediction_id} stalled in status '{status}' after {elapsed:.0f}s (max_poll_seconds={max_poll_seconds})", error_code=ProviderErrorCode.TIMEOUT)`.
   - Otherwise return `False`.

   Because the loop calls `poll()` through `_retry_phase`, a `TIMEOUT`-coded raise is retried at most `max_attempts` times (default 6, `poll_transient_retries=5 + 1`) — the ceiling stays breached across those attempts, so it deterministically exhausts the budget and propagates as a **terminal `FAILED` step** with `error_code=TIMEOUT`. `TIMEOUT` is in `RETRYABLE_ERROR_CODES`, so Samsar (or genblaze step-retry) can re-drive it — exactly what the issue asks for. Termination is bounded by `max_attempts`, **not** by the caller's `timeout`, which is the whole point.

3. **Provider constructors** — no change needed. `GMICloudVideoProvider`, `GMICloudImageProvider`, and `GMICloudAudioProvider` define **no** `__init__` of their own; they inherit `GMICloudBase.__init__`, so adding `max_poll_seconds` to the base automatically exposes it on all three (`GMICloudVideoProvider(max_poll_seconds=...)` works out of the box). `image.py` / `audio.py` were therefore **not** edited.

4. **Defensive `fetch_output` hardening** (`provider.py:85-126`): today the failure branch only matches `("failed", "cancelled")`; any other non-`success` status that somehow reaches fetch produces a vague `"completed but no video URL found"`. Change the guard to raise a clear terminal error for **any status that is not `success`** (carrying the status), so fetch can never silently mis-handle an unexpected state. Low-risk belt-and-suspenders; the ceiling means fetch is normally only reached on a genuine terminal status.

5. **README** (`libs/connectors/gmicloud/README.md`): document `max_poll_seconds` (default, meaning, `None` to disable) alongside `poll_interval` / `http_timeout`.

Reused, not reinvented: core's `_poll_cache_lock` thread-safe-dict idiom (`base.py:541-581`), `ProviderErrorCode.TIMEOUT` + `RETRYABLE_ERROR_CODES` (`enums.py:56,69`), the existing `map_gmicloud_error` / `retry_after` plumbing (unchanged).

## Risk

Low–moderate; connector-scoped, no core or public-API-signature-breaking change (new kwarg is keyword-only with a default).

- The ceiling changes observable behaviour: a job that previously hung "forever" now fails at ~`max_poll_seconds`. This is the intended fix; 1800s is well beyond normal Seedance completion, so legitimate jobs are unaffected.
- New per-instance state (`_poll_first_seen`) is bounded — entries are cleared on a terminal poll, with a FIFO cap (`_POLL_FIRST_SEEN_MAX = 512`) as the backstop for ids that only ever end via the ceiling. The entry is deliberately **not** cleared on ceiling breach: the core poll phase retries `poll()` a bounded number of times, and each retry must still see the deadline blown so the retry budget drains and the step terminates — clearing on breach would reset the clock and re-hang. Mutations are lock-guarded, matching the existing poll-cache concurrency contract, so fan-out (threads / `to_thread`) is safe.
- `estimated_seconds=30.0` on submit (`_base.py:265`) still delays the first poll — unchanged, and negligible against the ceiling.

## Files Modified

| File | Change |
| --- | --- |
| `libs/connectors/gmicloud/genblaze_gmicloud/_base.py` | Add `max_poll_seconds` param (default 1800s) + lock-guarded `_poll_first_seen`; enforce the stall ceiling in `poll()` via `_enforce_poll_deadline` / `_clear_poll_deadline` |
| `libs/connectors/gmicloud/genblaze_gmicloud/provider.py` | Harden `fetch_output` to fail on any non-`success` status |
| `libs/connectors/gmicloud/tests/test_gmicloud_provider.py` | 7 new tests, following the file's `MagicMock`-response convention |
| `libs/connectors/gmicloud/README.md` | Document `max_poll_seconds` |

**Not modified:** `image.py` / `audio.py` — neither defines its own `__init__`, so `max_poll_seconds` reaches `GMICloudImageProvider` / `GMICloudAudioProvider` automatically through the inherited `GMICloudBase.__init__` (no per-subclass threading required).

No production behaviour outside gmicloud changes. No version bump / CHANGELOG entry unless this ships in a release wave (per RELEASING.md / CONTRIBUTING.md).

## Tests (TDD — write failing first)

In `test_gmicloud_provider.py`:

- `test_poll_raises_timeout_when_stalled_past_ceiling`: construct provider with a tiny `max_poll_seconds` (or pre-seed `_poll_first_seen` to an old monotonic value); stub the GET to keep returning `{"status": "processing"}`; assert `poll()` raises `ProviderError` with `error_code == ProviderErrorCode.TIMEOUT` and a message naming the status + request id.
- `test_poll_does_not_raise_before_ceiling`: `processing` within the window → returns `False`, no raise.
- `test_poll_terminal_clears_first_seen`: a `success`/`failed` poll returns `True` and removes the tracking entry (no leak).
- `test_poll_ceiling_is_per_prediction_id`: two distinct ids tracked independently.
- `test_stalled_job_terminates_as_failed_step`: drive `provider.invoke(...)` with a stubbed always-`processing` poll and a tiny ceiling + large `config["timeout"]`; assert the step ends `StepStatus.FAILED` with `error_code == TIMEOUT` — the end-to-end proof that a wedged job no longer hangs regardless of caller timeout.
- `test_fetch_output_raises_on_unexpected_status`: `_fetch_detail` returns a non-`success`/non-`failed` status → terminal `ProviderError`.

## Verification

```bash
# regression gate — new stall tests must fail before the fix
cd libs/connectors/gmicloud && pytest tests/test_gmicloud_provider.py -k "stall or ceiling or timeout" -v

# full connector suite (expect the pre-existing capability opt-out skips only)
/test-package gmicloud    # or: cd libs/connectors/gmicloud && pytest tests/ -q -rs

# lint / format
ruff check libs/connectors/gmicloud/
ruff format --check libs/connectors/gmicloud/

# full-suite gate before done (CLAUDE.md)
make test
```

Manual sanity: temporarily set `max_poll_seconds` small and confirm a stubbed always-`processing` request produces a terminal `FAILED` step with `error_code=timeout` and an actionable message, rather than hanging.
