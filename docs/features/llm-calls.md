<!-- last_verified: 2026-07-28 -->
# Feature: LLM Calls

Thin standalone wrappers around OpenAI, Google Gemini, and GMICloud chat /
completion APIs. Returns a uniform `ChatResponse` so callers can swap
providers without rewriting response handling.

**Not** integrated with `Pipeline` / `Step` / `Asset` / manifest. Genblaze
is a media-generation framework; chat is a convenience for callers that
want to drive media steps from an LLM without taking a second LLM-routing
dependency. If you need manifest provenance for an LLM call, stash details
in `step.metadata` on the downstream media step, or wrap the call in your
own `SyncProvider` subclass.

## Surface

- `genblaze_openai.chat`, `genblaze_openai.achat`
- `genblaze_google.chat`, `genblaze_google.achat`
- `genblaze_gmicloud.chat`, `genblaze_gmicloud.achat`
- Models: `genblaze_core.models.chat.{ChatMessage, ToolCall, ChatResponse}`

## Signature

```python
chat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    *,
    prompt: str | None = None,
    system: str | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    api_key: str | None = None,
    client: Any = None,        # escape hatch
    retry_on_rate_limit: bool = False,   # openai / google only — see "Rate limits" below
    retry_policy: RetryPolicy | None = None,
    **kwargs,
) -> ChatResponse
```

`ChatResponse` carries `text`, `model`, `finish_reason`, `tokens_in/out`,
`tokens_cached`, `tool_calls`, `cost_usd`, `raw`.

## Usage

```python
from genblaze_openai import chat

resp = chat("gpt-4o", prompt="A cinematic sunset over Tokyo")
# resp.text, resp.tokens_out, resp.cost_usd
```

Compose with a media step manually:

```python
description = chat("gpt-4o", prompt="A cinematic sunset").text
pipe = Pipeline("hero").step(SoraProvider(), model="sora-2", prompt=description)
```

## Rate limits

**These helpers do not retry by default.** A 429 raises immediately — the
`ProviderError` carries a parsed `retry_after` hint (seconds), but callers
must act on it themselves. At archive scale (e.g. many vision calls over
video frames) this is the common case, not an edge case.

For `genblaze_openai.chat`/`achat` and `genblaze_google.chat`/`achat`, pass
`retry_on_rate_limit=True` to opt in to a bounded wait-and-retry loop that
honors the server's `Retry-After` hint (falling back to exponential backoff
with jitter when no hint is present):

```python
from genblaze_openai import chat

# Retries up to RetryPolicy()'s default 6 attempts, honoring each 429's
# `Retry-After` hint before raising.
resp = chat("gpt-4o-mini", messages=frame_messages, retry_on_rate_limit=True)
```

Pass a `genblaze_core.providers.retry.RetryPolicy` via `retry_policy=` to tune
the attempt cap, retryable-code set, or backoff timing (passing `retry_policy=`
alone, without `retry_on_rate_limit=True`, also opts in). `achat()` accepts the
same kwargs — the retry wait happens inside the worker thread `achat` already
runs in, so it never blocks the event loop.

**Known limits of this opt-in loop:**

- **Bounded but not tiny.** Worst case is `(max_attempts - 1) *
  MAX_RETRY_AFTER_SEC` — with the default policy (6 attempts, 120s cap), that's
  up to ~10 minutes if a misbehaving upstream returns the maximum `Retry-After`
  hint on every attempt. Pass a tighter `retry_policy=RetryPolicy(max_attempts=2)`
  if that's unacceptable for your call site.
- **Not a rate limiter.** This is a per-call retry wrapper, not a shared
  token-bucket / queue-level limiter. Many concurrent callers hitting the same
  TPM ceiling all see the same server `Retry-After` hint and wake in lockstep,
  which can immediately re-trip the limit. For sustained, high-concurrency
  archive runs, pace calls externally (e.g. a semaphore or a queue) in addition
  to (not instead of) `retry_on_rate_limit=True`.

## Limits (v1)

- No token streaming. Use the provider SDK directly if you need it.
- No cross-provider tool-definition normalization — `tools=` passes through
  to the provider's native shape.
- Multi-turn tool conversations against Gemini require dict messages in
  Gemini's native shape; canonical `ChatMessage.tool_calls` translation is
  outbound-text-only.
- Gemini's `system=` kwarg, when set, supersedes any system message in the
  `messages` list. OpenAI / GMICloud keep both (provider behavior).
- `cost_usd` is always `None` for all standalone `chat()` helpers
  (OpenAI, Gemini, GMICloud). Vendor prices drift too fast for static
  tables to stay accurate, and `chat()` has no model registry. Compute
  cost from `tokens_in`/`tokens_out` using your own rates (see
  `docs/reference/pricing-recipes.md`). `PricingContext` populates cost
  only on the Pipeline-Step provider path, not here.
- Model ids pass through verbatim — unknown models aren't blocked
  client-side. Matches the "unknown models pass through" convention used
  by the media provider classes.
- Errors are wrapped in `ProviderError` with a classified `error_code`.

## Verification

- `libs/core/tests/unit/test_chat_models.py`
- `libs/connectors/{openai,google,gmicloud}/tests/test_chat.py`
- Quick: `cd libs/connectors/openai && pytest tests/test_chat.py -v`
- Full: `make test`
