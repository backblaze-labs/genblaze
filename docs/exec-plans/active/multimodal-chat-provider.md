<!-- created: 2026-04-28 -->
# Multimodal Chat Provider

Land Pipeline-step semantics for NIM chat (multimodal) plus the cross-provider
primitives that make it possible without one-off shims. Drives the NVIDIA
connector feedback (Tier 1 + Tier 2) without inventing a parallel hierarchy.

Sequenced **after** `p0-p1-feedback-execution.md` Wave 0 (conformance suite),
Wave 2A (`BaseSink.emit_bytes`), and Wave 4 (`Pipeline.input` + `Asset.text`).
This plan adds three small PRs and two one-line clarifications to those waves.

## Goals

- Author of the NVIDIA-connector feedback can run `pipeline.input(asset).step(NvidiaChatProvider(...), ...).run()` end-to-end with multimodal input and typed JSON output, no shim.
- Zero net new abstractions. No `ChatProvider` base class for one consumer; no `Modality.DOCUMENT`; no parallel `Pipeline.step(input=)` attachment surface; no backend-level `stage_upload`.
- Every cross-provider primitive (typed message content, structured-output kwarg) lands in core/connectors so the next chat surface — Whisper / Gemini chat / OpenAI multimodal — adopts the same shape on day one.

**Done when:** the four `_normalize_messages` impls accept the typed content union without breaking, `NvidiaChatProvider` ships with verified NIM image+video+reasoning support, and `chat(response_format=...)` works on OpenAI/NIM/GMICloud helpers.

## What we're explicitly **not** doing

| Dropped | Why |
|---|---|
| `Modality.DOCUMENT` enum value | `Modality` is `Step.modality` (output) and a closed wire enum. PDF *input* is `Asset.media_type="application/pdf"` — an asset attribute, not a modality. |
| `route_documents()` input mapping helper | `route_*` packs `step.inputs` into native param dicts; chat content blocks are a different abstraction. |
| `DocumentURLContent` content block | NIM/Nemotron does not have a document content block. Per the [HuggingFace Nemotron 3 Nano Omni post](https://huggingface.co/blog/nvidia/nemotron-3-nano-omni-multimodal-intelligence), PDFs are processed as multi-page image sequences — rasterized client-side and sent as N `image_url` blocks. |
| `AudioURLContent` block (this PR) | `audio_url` (URL ref) is NIM/vLLM; `input_audio` (base64) is OpenAI. Not portable yet. Deferred to a follow-up once we have two verified consumers. |
| `ChatProvider(SyncProvider)` base class | One concrete consumer (NVIDIA). Promote when a second `*ChatProvider` lands. The four existing `chat()` helpers stay as standalone module-level functions. |
| ~~`Pipeline.step(input=...)` kwarg~~ | **REOPENED 2026-04-28 → SHIPPED as `Pipeline.step(external_inputs=[Asset, ...])`.** Original "drop" rationale was that it duplicated `Pipeline.input()` (Wave 4). But Wave 4 was blocked behind the `@genblaze/spec` 0.4.0 schema change and `NvidiaChatProvider` shipped first with no public path to seed `step.inputs` on step 0. Renamed `input` → `external_inputs` to avoid collision with `Step.seed`/`**params` swallow risk and to communicate "outside-the-graph injection." Wave 4's planned `Pipeline.input(asset_or_path)` will be sugar over this primitive, not a replacement. See `[Unreleased]` CHANGELOG entry. |
| `S3StorageBackend.stage_upload(...)` | Same primitive as `BaseSink.emit_bytes()` (active plan Wave 2A) at the wrong layer. Backend shouldn't know about `Asset`. See clarification §C.1. |

## Cross-plan clarifications (one-line amendments)

### C.1 — Wave 2A `BaseSink.emit_bytes()` must populate `sha256` + `media_type`

The active plan describes `emit_bytes(...) -> Asset` returning a "populated Asset." Make explicit: the returned `Asset` MUST have `sha256` and `media_type` set so `step_cache_key`'s `a.sha256 or a.url` branch picks the stable hash, not the rotating presigned URL. Without this the user-uploaded-asset cache-stability story breaks and the original feedback's `stage_upload` proposal returns under a different name.

### C.2 — Wave 4 `Pipeline.input()` accepts `Asset`

The active plan describes `Pipeline.input(asset_or_path)` accepting `str | Path`. Confirm the signature accepts `Asset` directly so a `sink.emit_bytes(...)` result chains cleanly: `pipeline.input(sink.emit_bytes(pdf_bytes, media_type="application/pdf"))`. Both clarifications are one-line; flag in the C.1/C.2 PR descriptions, no separate PR needed.

## PR 1 — Typed multimodal `ChatMessage.content`

**Files:**
- `libs/core/genblaze_core/models/chat.py` — type change + new content-block classes.
- `libs/connectors/openai/genblaze_openai/chat.py` — `_normalize_messages`.
- `libs/connectors/nvidia/genblaze_nvidia/chat.py` — `_normalize_messages`.
- `libs/connectors/gmicloud/genblaze_gmicloud/chat.py` — `_normalize_messages`.
- `libs/connectors/google/genblaze_google/chat.py` — `_normalize_to_gemini`.

**Design:**

```python
# libs/core/genblaze_core/models/chat.py

class TextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ImageURLRef(BaseModel):
    url: str
    detail: Literal["low", "high", "auto"] | None = None

class ImageURLContent(BaseModel):
    type: Literal["image_url"] = "image_url"
    image_url: ImageURLRef

class VideoURLRef(BaseModel):
    url: str

class VideoURLContent(BaseModel):
    type: Literal["video_url"] = "video_url"
    video_url: VideoURLRef

ContentBlock = Annotated[
    TextContent | ImageURLContent | VideoURLContent,
    Field(discriminator="type"),
]

class ChatMessage(BaseModel):
    role: ChatRole
    content: str | list[ContentBlock] = ""
    # ... existing fields unchanged
```

**Decision: no auto-wrap validator + `content_blocks` accessor property.** Plain `str` content stays `str` post-validation; connectors branch on `isinstance(m.content, str)` for wire fidelity (string-content has cheaper wire shape and avoids provider quirks with array-content system messages). Generic content-processing code uses the property:

```python
class ChatMessage(BaseModel):
    content: str | list[ContentBlock] = ""

    @property
    def content_blocks(self) -> list[ContentBlock]:
        """Materialized view — strings become [TextContent(...)]; lists pass through."""
        if isinstance(self.content, str):
            return [TextContent(text=self.content)] if self.content else []
        return self.content
```

Storage stays as the user wrote it (no surprising mutation); generic callers iterate `msg.content_blocks` for a unified shape.

**`_normalize_messages` updates** (each connector, same shape):

```python
# nvidia / openai / gmicloud — all OpenAI-wire-compatible
content = m.content
if isinstance(content, list):
    msg["content"] = [block.model_dump(exclude_none=True) for block in content]
else:
    msg["content"] = content
```

**Google (Gemini) translation:**
- `TextContent` → `{"text": "..."}`. Easy.
- `ImageURLContent` / `VideoURLContent` → **raise `ProviderError(INVALID_INPUT)` with a precise message.** Gemini's chat API does not accept plain HTTP URLs for media; it requires `inline_data` (base64) or `file_data` (Google File API URI). Pretending the OpenAI-vision shape translates would error mid-runtime instead of at construction. Full Gemini multimodal support is tracked as a P1 follow-up in `framework-dx-recommendations.md`. The error message:

  ```
  Gemini does not accept {ImageURLContent|VideoURLContent}. Pass media as
  inline_data (base64) or file_data (File API URI) via raw dict messages,
  or wait for genblaze-google's typed multimodal support (tracked in
  framework-dx-recommendations.md).
  ```

  Users hitting this can pass `messages: list[dict]` directly to `genblaze_google.chat()` today (it accepts raw dicts) — they're routed around the typed path, not blocked.

**Tests:** `libs/core/tests/unit/test_chat_message_content.py`
- Plain `str` content round-trips unchanged.
- List-of-blocks content validates with discriminator.
- Unknown block type raises pydantic ValidationError.
- Each connector's `_normalize_messages` produces the expected wire dict for both shapes.

**Conformance suite extension:** Wave 0's suite gains a parametrized test asserting each connector's `chat()` accepts `[TextContent(text="hi")]` without error (skip when the connector isn't installed).

**LOC:** ~250.

## PR 2 — `NvidiaChatProvider(SyncProvider)`

**Files:**
- `libs/connectors/nvidia/genblaze_nvidia/chat_provider.py` — new module.
- `libs/connectors/nvidia/genblaze_nvidia/__init__.py` — export.
- `libs/connectors/nvidia/genblaze_nvidia/models/` — Nemotron registry slice.
- `libs/connectors/nvidia/pyproject.toml` — entry point.
- `libs/connectors/nvidia/tests/unit/test_chat_provider.py`.

**Design:** direct `SyncProvider` subclass — no abstract base. NVIDIA-specific knobs live on the class, not in core.

```python
class NvidiaChatProvider(SyncProvider):
    """NIM chat as a Pipeline step. Multimodal input via step.inputs[Asset]."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        reasoning: bool | None = None,             # tri-state: None = server default
        media_io_kwargs: dict | None = None,       # NIM video controls (fps, num_frames)
        mm_processor_kwargs: dict | None = None,   # NIM image tiling
        models: ModelRegistry | None = None,
    ):
        super().__init__(models=models or self.models_default())
        ...

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.TEXT],
            supported_inputs=["text", "image", "video"],
            accepts_chain_input=True,
        )

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        messages = self._build_messages(step)        # builds list[ContentBlock] from step.inputs
        payload = self._payload(step, messages)
        # Tri-state reasoning: only override server default when explicitly set.
        # Reasoning-suffixed checkpoints default thinking-on; base checkpoints
        # default thinking-off. Hard-coding True client-side fights server.
        if self._reasoning is not None:
            payload.setdefault("extra_body", {})
            payload["extra_body"].setdefault("chat_template_kwargs", {})
            payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] = self._reasoning
        # ... thread response_format if step.params has it
        # ... call NIM via openai SDK with base_url override (reuses chat.py helpers)
        # populate step.assets = [Asset(text=response.text, media_type="text/plain", sha256=...)]
        # cost_usd is None (RPM-gated free tier; enterprise is contract-specific);
        # token usage lives in step.provider_payload["usage"] for downstream cost calc
        step.cost_usd = None
        step.provider_payload["usage"] = {
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "tokens_cached": response.tokens_cached,
        }
        return step
```

**Multimodal message builder:**

```python
def _build_messages(self, step: Step) -> list[ChatMessage]:
    user_blocks: list[ContentBlock] = []
    if step.prompt:
        user_blocks.append(TextContent(text=step.prompt))
    for asset in step.inputs:
        mt = asset.media_type or ""
        if mt.startswith("image/"):
            user_blocks.append(ImageURLContent(image_url=ImageURLRef(url=asset.url)))
        elif mt.startswith("video/"):
            user_blocks.append(VideoURLContent(video_url=VideoURLRef(url=asset.url)))
        elif mt == "application/pdf":
            # PDFs are not natively supported by NIM. Document the rasterization
            # pattern in the README: caller renders pages to images first.
            raise ProviderError(
                "PDF input not natively supported by NIM. Rasterize pages to "
                "images and pass each as Asset(media_type='image/png'). See "
                "genblaze-nvidia README for the helper recipe.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        else:
            raise ProviderError(
                f"Unsupported input media type for NIM chat: {mt!r}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
    return [ChatMessage(role="user", content=user_blocks if user_blocks else "")]
```

**Reasoning kwarg (tri-state).** `reasoning: bool | None = None`. `None` (default) means "do not send the kwarg — let the server pick based on the model checkpoint." Reasoning-suffixed checkpoints (`...-Reasoning-BF16`) default thinking-on server-side; base checkpoints default thinking-off. Hard-coding `True` or `False` client-side overrides the server's correct per-model decision. `True` / `False` set `extra_body["chat_template_kwargs"]["enable_thinking"]` explicitly. **Verified upstream kwarg name is `enable_thinking`**, not `thinking` as the original feedback wrote — NVIDIA blog example: `extra_body={"chat_template_kwargs": {"enable_thinking": True}}`.

**Output asset.** `step.assets = [Asset(text=response.text, media_type="text/plain", sha256=sha256(response.text))]`. Depends on Wave 4 `Asset.text` from the active plan — gate this PR on Wave 4 landing. If Wave 4 slips, fall back to `step.metadata["text"] = response.text` and document the upgrade path.

**ModelRegistry slice.** Register Nemotron 3 Nano Omni and Nemotron Nano VL families with `pricing=None`. Free tier `integrate.api.nvidia.com` is RPM-gated, not per-token; enterprise NIM is contract-specific. Faking a price would be misleading. Token counts (`tokens_in`, `tokens_out`, `tokens_cached`) ARE always populated via `step.provider_payload["usage"]` — downstream cost-tracking computes `tokens × negotiated_rate` per `ModelRegistry.fork(pricing=...)`. When NVIDIA publishes public per-token rates, attaching a `PricingStrategy` is a one-line registry change with no API break.

**Wire-shape verification before merge.** The PR description must include a `curl` against `https://integrate.api.nvidia.com/v1/chat/completions` with image+video content blocks against the actual Nemotron Omni model id, capturing a 200 response. If video doesn't work on a NIM-hosted Nemotron Omni endpoint at merge time, ship image-only and add video in a follow-up — don't ship a primitive that errors at runtime.

**Tests:** mocked `openai.OpenAI` client. Cases:
- Image input → `image_url` content block in payload.
- Video input → `video_url` content block.
- `reasoning=False` → `extra_body` carries `enable_thinking=False`.
- `media_io_kwargs={"video": {"fps": 3.0}}` propagated to payload.
- Unsupported media type raises `ProviderError(INVALID_INPUT)`.
- PDF media type raises with the rasterization message.
- Conformance suite picks it up automatically.

**LOC:** ~400.

## PR 3 — `response_format` on OpenAI-wire chat helpers

**Files:**
- `libs/connectors/openai/genblaze_openai/chat.py`
- `libs/connectors/nvidia/genblaze_nvidia/chat.py`
- `libs/connectors/gmicloud/genblaze_gmicloud/chat.py`
- New shared helper `libs/core/genblaze_core/models/chat.py` (or `_chat_helpers.py`).

**Design:**

```python
def chat(
    model: str,
    ...,
    response_format: dict | type[BaseModel] | None = None,
    ...,
) -> ChatResponse:
    ...
    if response_format is not None:
        payload["response_format"] = _coerce_response_format(response_format)
```

Shared helper in core:

```python
def _coerce_response_format(rf: dict | type[BaseModel]) -> dict:
    """Accept a Pydantic class (auto-schema) or a dict (passthrough)."""
    if isinstance(rf, type) and issubclass(rf, BaseModel):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": rf.__name__,
                "schema": rf.model_json_schema(),
                "strict": True,
            },
        }
    return rf  # raw dict — caller knows the wire shape
```

Lives in `libs/core/genblaze_core/models/chat.py` so all three connectors import it. Stays Pydantic v2 only — no extra deps.

**Gemini deferred.** Gemini's structured output uses `response_schema` on `GenerationConfig` plus `response_mime_type="application/json"`. Different translation, not portable. Tracked as a follow-up in `framework-dx-recommendations.md`. Document the gap in the Gemini connector README.

**Tests:** `libs/core/tests/unit/test_response_format_coerce.py` — Pydantic class produces a `json_schema` dict; raw dict passthrough; non-BaseModel class raises `TypeError`. Each connector test confirms the kwarg lands in the request payload.

**LOC:** ~150.

## Sequencing

1. **PR 1** (typed content) — atomic across four connectors. No external deps.
2. **PR 3** (`response_format`) — independent of PR 1, can land in parallel.
3. **PR 2** (`NvidiaChatProvider`) — depends on PR 1 (uses `ContentBlock` types), Wave 4 (uses `Asset.text`), and clarification C.1 (uses `BaseSink.emit_bytes` for stable cache keys via the user's upload path). Land last.

Estimated total: ~800 LOC + ~200 LOC tests. Three PRs, each ≤ 500 LOC.

## Tier 3 stragglers → `framework-dx-recommendations.md`

Add as separate rows (not part of this plan):

| Item | Effort | Priority |
|---|---|---|
| `PipelineError.failed_step_error_code: ProviderErrorCode` property | 0.5 day | P2 |
| `Pipeline.__len__` / `step_count` property | 0.5 day | P2 |
| `Pipeline.estimated_cost` as `@property` | 0.5 day | P2 |
| `BaseProvider.preflight_auth` returns `PreflightResult.SKIPPED` from default impl | 0.5 day | P2 |
| `Pipeline.stream(on_error=callback)` for mid-stream failure handling | 1 day | P2 |
| `NvidiaAudioProvider.list_voices()` populated for Riva | 1 day | P2 |
| `chat(response_format=...)` Gemini follow-up | 1 day | P2 |
| `AudioURLContent` block + audio routing once second consumer verified | 1 day | P3 |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| NIM Nemotron Omni rejects `video_url` at merge time | Wire-shape verification gate in PR 2 description; ship image-only fallback if needed. |
| Gemini's `_normalize_to_gemini` translation for `ImageURLContent` requires MIME type the user didn't pass | Add `media_type` field on `ImageURLRef`, optional, with a docstring noting Gemini requires it. Connector raises a clear error when missing. |
| `m.content` becoming a union breaks third-party callers reading `msg.content` as `str` | The non-auto-wrap design preserves `str` for callers passing strings. Document in CHANGELOG that callers passing `list[Block]` now get a list back; old callers unaffected. |
| `Asset.text` (Wave 4) slips → PR 2 blocked | PR 2 ships with metadata-fallback branch under a feature flag; unflipped until Wave 4 lands. |
| Auto-schema from `BaseModel` produces a schema NIM/OpenAI rejects (e.g., `additionalProperties` defaults differ) | Test against real NIM endpoint in PR 3; add `strict=True` and document the Pydantic v2 schema-tuning incantation in the connector README. |

## Resolved decisions

1. **`ChatMessage.content` shape — no auto-wrap + `content_blocks` accessor property.** Storage stays as the user wrote it (string round-trips to string; list round-trips to list). Generic content-processing code uses the materialized `msg.content_blocks` view. Rationale: backward compat with every existing caller, cheaper wire shape for text-only content, avoids provider quirks around array-content system messages.

2. **Gemini multimodal — raise `INVALID_INPUT` on non-text blocks in PR 1.** Gemini's chat API does not accept plain HTTP URLs for media (requires `inline_data` base64 or `file_data` File API URI). The OpenAI-vision shape simply does not translate. Full Gemini multimodal support tracked as a P1 follow-up; users today route around via raw `messages: list[dict]`.

3. **Nemotron pricing — `pricing=None` at registry default.** Free tier is RPM-gated, not per-token; enterprise is contract-specific. Token counts always populated via `step.provider_payload["usage"]` so downstream cost-tracking works. Forward-compatible: a future `PricingStrategy` attaches via `ModelRegistry.fork()` with no API break.

4. **`reasoning` default — tri-state `bool | None = None`.** Default `None` does not send `enable_thinking` to the server, letting NIM pick based on the model checkpoint (reasoning-suffixed defaults thinking-on; base defaults thinking-off). `True` / `False` override explicitly. Aligns with how `temperature`, `top_p`, `max_tokens` already behave in the SDK and avoids client-side defaults fighting server-side per-model defaults.
