<!-- last_verified: 2026-04-24 -->
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

## Limits (v1)

- No token streaming. Use the provider SDK directly if you need it.
- No cross-provider tool-definition normalization — `tools=` passes through
  to the provider's native shape.
- Multi-turn tool conversations against Gemini require dict messages in
  Gemini's native shape; canonical `ChatMessage.tool_calls` translation is
  outbound-text-only.
- Gemini's `system=` kwarg, when set, supersedes any system message in the
  `messages` list. OpenAI / GMICloud keep both (provider behavior).
- Pricing tables: OpenAI (gpt-4o family, gpt-4.1, gpt-4-turbo, o-series,
  gpt-3.5), Gemini (1.5 / 2.0 / 2.5 families). GMICloud returns
  `cost_usd=None` — fleet shifts faster than a table tracks.
- Model ids pass through verbatim — unknown models aren't blocked
  client-side, they just get `cost_usd=None` until registered. Matches
  the "unknown models pass through" convention used by the media
  provider classes.
- Errors are wrapped in `ProviderError` with a classified `error_code`.

## Verification

- `libs/core/tests/unit/test_chat_models.py`
- `libs/connectors/{openai,google,gmicloud}/tests/test_chat.py`
- Quick: `cd libs/connectors/openai && pytest tests/test_chat.py -v`
- Full: `make test`
