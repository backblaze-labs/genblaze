<!-- completed: 2026-04-24 -->
# Text modality — standalone callable across providers

> **Shipped in 0.2.4.** `ChatMessage` / `ToolCall` / `ChatResponse` models, `chat()` / `achat()` wrappers for `genblaze-openai`, `genblaze-google`, and `genblaze-gmicloud`, plus `docs/features/llm-calls.md` all landed. See CHANGELOG [0.2.4] "Added" section. Media-generation core untouched as designed.

## Goal

Let users call OpenAI, Google Gemini, and GMICloud chat / completion APIs through one uniform signature, **without** integrating text into `Step` / `Pipeline` / `Asset` / manifest. Media-generation core stays untouched.

## Non-goals (deferred until a real user asks)

- Pipeline-integrated text Step / Provider class
- Manifest-recorded LLM call provenance
- Token streaming events / new `StreamEvent` variants
- StepCache for chat calls
- Cross-provider tool-call schema normalization
- Vision / multi-modal chat input
- Spec / TS-type generation for chat models (these are function return types, not wire protocol)

## What ships

### 1. `libs/core/genblaze_core/models/chat.py` (new)

Pydantic v2 models, no business logic:

- `ChatMessage` — `role` (`system`/`user`/`assistant`/`tool`), `content: str`, optional `name`, `tool_call_id`, `tool_calls`.
- `ToolCall` — `id`, `name`, `arguments: dict[str, Any]`.
- `ChatResponse` — `text`, `model`, `finish_reason`, `tokens_in`, `tokens_out`, `tokens_cached`, `tool_calls`, `cost_usd`, `raw` (provider's raw response, escape hatch).

Wired through `genblaze_core.models.__init__` and the lazy-import map in `genblaze_core.__init__`.

### 2. `libs/connectors/openai/genblaze_openai/chat.py` (new)

```python
def chat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    *,
    prompt: str | None = None,
    system: str | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    api_key: str | None = None,
    timeout: float = 60.0,
    **kwargs: Any,
) -> ChatResponse: ...

async def achat(...) -> ChatResponse: ...   # asyncio.to_thread wrapper, matches base provider pattern
```

Wraps `openai.OpenAI().chat.completions.create(...)`. Raises `ProviderError` with classified `error_code` (re-uses `map_openai_error`).

Static rate table for cost calc covering `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4-turbo`, `gpt-3.5-turbo`. Unknown model → `cost_usd=None` (matches GMICloud "unknown models pass through" precedent).

### 3. `libs/connectors/google/genblaze_google/chat.py` (new)

Same signature. Translates canonical messages → Gemini's `contents` / `parts` and `system_instruction` shape internally. Wraps `client.models.generate_content(...)`. Raises `ProviderError` via `map_google_error`.

Rate table covers `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-1.5-pro`, `gemini-1.5-flash`.

### 4. `libs/connectors/gmicloud/genblaze_gmicloud/chat.py` (new)

Same signature. Posts to GMICloud's OpenAI-compatible chat endpoint via `httpx`. Raises `ProviderError` via `map_gmicloud_error`. Pricing returns `None` (GMI hosts a moving fleet of open-source models — no built-in table; users override per-call by inspecting `tokens_in/out` themselves).

### 5. Tests

- `libs/core/tests/unit/test_chat_models.py` — Pydantic round-trips, role validation, tool-call coercion.
- `libs/connectors/{openai,google,gmicloud}/tests/test_chat.py` — mocked SDK / HTTP, covering: `prompt` shorthand, `system` shorthand, `messages` list, tool calls in response, error → `ProviderError`, cost calc.

### 6. Docs

- `docs/features/llm-calls.md` — feature page.
- `ARCHITECTURE.md` features list — add link.
- `AGENTS.md` doc map — implicit (feature is under `docs/features/`).
- `CHANGELOG.md` — entry under unreleased.

## Files touched / created

| Path | Kind |
|---|---|
| `docs/exec-plans/active/text-modality-standalone.md` | new (this file) |
| `libs/core/genblaze_core/models/chat.py` | new |
| `libs/core/genblaze_core/models/__init__.py` | edit (export new models) |
| `libs/core/genblaze_core/__init__.py` | edit (lazy-import map) |
| `libs/core/tests/unit/test_chat_models.py` | new |
| `libs/connectors/openai/genblaze_openai/chat.py` | new |
| `libs/connectors/openai/genblaze_openai/__init__.py` | edit (export) |
| `libs/connectors/openai/tests/test_chat.py` | new |
| `libs/connectors/google/genblaze_google/chat.py` | new |
| `libs/connectors/google/genblaze_google/__init__.py` | edit |
| `libs/connectors/google/tests/test_chat.py` | new |
| `libs/connectors/gmicloud/genblaze_gmicloud/chat.py` | new |
| `libs/connectors/gmicloud/genblaze_gmicloud/__init__.py` | edit |
| `libs/connectors/gmicloud/tests/test_chat.py` | new |
| `docs/features/llm-calls.md` | new |
| `ARCHITECTURE.md` | edit (features list) |
| `CHANGELOG.md` | edit |

## Non-disruption guarantee

| Surface | Touched? |
|---|---|
| `BaseProvider`, `SyncProvider`, lifecycle machinery | No |
| `Step`, `Asset`, `Manifest`, `Run` | No |
| Canonical JSON, hashing | No |
| `StreamEvent`, `Tracer`, `libs/spec/` | No |
| `Pipeline`, sinks, storage, media handlers | No |
| `ModelRegistry`, `pricing.py` | No |

Only positive additions. Removing this feature later = delete six files.

## Verification

- `make test` — full gate, all 13 packages
- `make lint`
- Smoke: `python -c "from genblaze_openai import chat; print(chat.__doc__)"`

## Future expansion (only if asked)

- `chat_stream()` — iterator of token deltas (additive)
- `ChatProvider(SyncProvider)` — Pipeline-integrated wrapper that internally calls `chat()` (additive, ~40 lines per connector)
- Vision / image content parts in `ChatMessage.content` (extend `content: str` → `str | list[ContentPart]`)
- 4th provider (Anthropic, Mistral) — same shape, ~80-line file
