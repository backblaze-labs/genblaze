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
own `SyncProvider` subclass (recipe below).

`Pipeline.step()` requires a `BaseProvider` instance and raises `TypeError`
immediately if you pass `chat`/`achat` directly — they're plain functions,
not providers, so there's nothing for `Pipeline.run()` to call `invoke()` on.

## Recording a chat step's provenance

To make a script-writing `chat()` call appear as a step in the manifest
(so provenance covers the words as well as the downstream media), wrap it
in a small local `SyncProvider`:

```python
import hashlib

from genblaze_core import Asset, Pipeline, SyncProvider
from genblaze_openai import chat

class ChatStep(SyncProvider):
    name = "openai-chat"

    def generate(self, step, config=None):
        resp = chat(step.model, prompt=step.prompt)
        digest = hashlib.sha256(resp.text.encode("utf-8")).hexdigest()
        # metadata["text"] is what Pipeline's own moderation code reads
        # (_input_text_payloads) for textual assets. url="text:<sha256>" is
        # just a stable, content-addressed identifier for provenance — it
        # is never fetched, so it's fine that it isn't https/file (an
        # inline data: URI would work too, but has undefined sink behavior).
        step.assets.append(
            Asset(url=f"text:{digest}", media_type="text/plain", sha256=digest,
                  metadata={"text": resp.text})
        )
        return step

# Run the chat step on its own — input_from=[...] chaining fetches
# step.inputs by URL (https/file only, enforced for SSRF safety by
# validate_chain_input_url) and can't be used to hand raw text to the
# next step. Instead, read the text back out of the completed step and
# pass it as a normal prompt= to the next pipeline.
chat_result = (
    Pipeline("narration-script")
    .step(ChatStep(), model="gpt-4o", prompt="Write a one-line narration")
    .run()
)
script = chat_result.run.steps[0].assets[0].metadata["text"]

tts_result = (
    Pipeline("narration-audio")
    .from_result(chat_result)  # links parent_run_id for provenance lineage
    .step(TtsProvider(), model="tts-1", prompt=script)
    .run()
)
```

`ChatStep` only needs `generate()` — `SyncProvider` handles the rest of the
submit/poll/fetch_output lifecycle. `Pipeline.from_result()` links the two
runs via `parent_run_id` so the manifest for the audio step points back at
the run that produced its script. This is a documentation recipe, not a
built-in class; there is no first-party text/chat `BaseProvider` today (see
`docs/exec-plans/active/multimodal-chat-provider.md`).

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
