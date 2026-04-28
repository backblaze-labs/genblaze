"""Shared chat-call models — used by `chat()` callables in connector packages.

These types are *not* part of the manifest wire protocol. They are function
return types for the standalone `chat()` / `achat()` entry points exposed by
`genblaze_openai`, `genblaze_google`, `genblaze_gmicloud`, and `genblaze_nvidia`.
Keeping the shape uniform across connectors lets callers swap providers without
rewriting response-handling code.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

ChatRole = Literal["system", "user", "assistant", "tool"]


class TextContent(BaseModel):
    """Plain-text content block. Type tag matches OpenAI/NIM wire."""

    type: Literal["text"] = "text"
    text: str


class ImageURLRef(BaseModel):
    """Image reference: URL plus optional detail / mime hints."""

    url: str = Field(description="Public URL or `data:` URI for the image.")
    detail: Literal["low", "high", "auto"] | None = Field(
        default=None, description="OpenAI/NIM detail hint; ignored by providers that lack it."
    )
    media_type: str | None = Field(
        default=None,
        description=(
            "Optional MIME type override (e.g. 'image/png'). Some providers — notably "
            "Gemini's `file_data` shape — require it; OpenAI/NIM ignore it."
        ),
    )


class ImageURLContent(BaseModel):
    """Image content block in OpenAI-vision wire shape (`image_url`)."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURLRef


class VideoURLRef(BaseModel):
    """Video reference: URL plus optional MIME hint."""

    url: str = Field(description="Public URL for the video asset.")
    media_type: str | None = Field(
        default=None, description="Optional MIME type override (e.g. 'video/mp4')."
    )


class VideoURLContent(BaseModel):
    """Video content block in NIM/vLLM wire shape (`video_url`).

    Not portable to OpenAI (no native video on chat completions today).
    Connectors that don't support it raise `INVALID_INPUT` at translation.
    """

    type: Literal["video_url"] = "video_url"
    video_url: VideoURLRef


class AudioURLRef(BaseModel):
    """Audio reference: URL plus optional MIME hint.

    URL accepts the same forms NIM accepts for image/video: a public HTTPS
    URL, a local ``file://`` URI, or an inline ``data:audio/...;base64,...``
    URI. Verified against the Nemotron-3-Nano-Omni model card.
    """

    url: str = Field(description="Public, file://, or data: URL for the audio asset.")
    media_type: str | None = Field(
        default=None, description="Optional MIME type override (e.g. 'audio/wav', 'audio/mpeg')."
    )


class AudioURLContent(BaseModel):
    """Audio content block in NIM/vLLM wire shape (``audio_url``).

    OpenAI's chat-completions audio uses ``input_audio`` (base64 in body)
    instead — when that block lands, it'll be a sibling discriminated-union
    variant. Connectors that don't accept ``audio_url`` will get a 400 from
    upstream; it isn't refused locally so callers see the real provider
    error message.
    """

    type: Literal["audio_url"] = "audio_url"
    audio_url: AudioURLRef


# Discriminated union of multimodal content blocks. Adding a block type
# is additive — pydantic dispatches on `type` and existing callers passing
# string content stay untouched.
ContentBlock = Annotated[
    TextContent | ImageURLContent | VideoURLContent | AudioURLContent,
    Field(discriminator="type"),
]


def coerce_response_format(rf: dict | type[BaseModel]) -> dict:
    """Coerce a `response_format=` value to the OpenAI-wire JSON-schema dict.

    Accepts:
    - A Pydantic v2 ``BaseModel`` subclass — the JSON Schema is auto-generated
      via ``model_json_schema()`` and wrapped in the ``{"type":"json_schema",
      "json_schema":{...}}`` envelope OpenAI / NIM / GMICloud all expect.
    - A pre-formed dict — passthrough; the caller knows the wire shape.

    Used by every OpenAI-wire-compatible ``chat()`` helper. Gemini follow-up
    will translate from BaseModel to its own ``response_schema`` shape via a
    separate function — do not extend this one to dispatch by provider.
    """
    if isinstance(rf, type) and issubclass(rf, BaseModel):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": rf.__name__,
                "schema": rf.model_json_schema(),
                "strict": True,
            },
        }
    if isinstance(rf, dict):
        return rf
    raise TypeError(
        f"response_format must be a dict or a pydantic BaseModel subclass; got {type(rf).__name__}"
    )


class ToolCall(BaseModel):
    """A single tool / function call requested by the model."""

    id: str = Field(description="Provider-assigned call ID; echoed back in tool result messages.")
    name: str = Field(description="Tool / function name the model is calling.")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Parsed JSON arguments. Providers that return a JSON string are coerced here.",
    )

    @field_validator("arguments", mode="before")
    @classmethod
    def _coerce_arguments(cls, value: Any) -> Any:
        """Accept JSON-string arguments (OpenAI's wire shape) and parse to dict."""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return {"_raw": value}
            return parsed if isinstance(parsed, dict) else {"_raw": value}
        return value


class ChatMessage(BaseModel):
    """Canonical chat turn. Connectors translate to / from native shapes.

    `content` accepts either a plain string (95% case — text-only turns) or a
    list of typed `ContentBlock`s for multimodal input (image / video / future
    audio). Storage round-trips as the caller wrote it: passing a string keeps
    a string; passing a list keeps a list. For generic processing that wants a
    single shape, use the `content_blocks` property which materializes string
    content as `[TextContent(...)]`.
    """

    role: ChatRole
    content: str | list[ContentBlock] = Field(
        default="",
        description=(
            "Text content (str) or multimodal blocks (list). Empty string for "
            "assistant tool-call turns."
        ),
    )
    name: str | None = Field(
        default=None,
        description="For role='tool', the function name that produced this result.",
    )
    tool_call_id: str | None = Field(
        default=None,
        description="For role='tool', matches the assistant's prior ToolCall.id.",
    )
    tool_calls: list[ToolCall] | None = Field(
        default=None,
        description="For role='assistant', tool calls the model decided to make this turn.",
    )

    @property
    def content_blocks(self) -> list[ContentBlock]:
        """Materialized view: strings become `[TextContent(...)]`; lists pass through.

        Use this when content-processing code wants a single shape; use `content`
        directly when you need to preserve the caller's str-vs-list choice for
        wire fidelity.
        """
        if isinstance(self.content, str):
            return [TextContent(text=self.content)] if self.content else []
        return self.content


class ChatResponse(BaseModel):
    """Uniform chat response across providers.

    Pricing and tool-call shape are normalized; provider-specific fields land
    in `raw` for callers that need the native payload.
    """

    text: str = Field(default="", description="Final assistant text. Empty when only tool calls.")
    model: str = Field(description="Model id the call resolved to (post-alias).")
    finish_reason: str | None = Field(
        default=None,
        description=(
            "Provider finish reason ('stop', 'length', 'tool_calls', 'content_filter', ...)."
        ),
    )
    tokens_in: int | None = Field(default=None, description="Input / prompt token count.")
    tokens_out: int | None = Field(default=None, description="Output / completion token count.")
    tokens_cached: int | None = Field(
        default=None, description="Cached prompt tokens billed at the cached rate, when reported."
    )
    tool_calls: list[ToolCall] = Field(
        default_factory=list, description="Tool calls the model emitted (empty when none)."
    )
    cost_usd: float | None = Field(
        default=None,
        description="Estimated USD cost from the connector's static rate table; None if unknown.",
    )
    raw: dict[str, Any] = Field(
        default_factory=dict, description="Provider's raw response dict — escape hatch."
    )

    def __repr__(self) -> str:
        snippet = (self.text[:60] + "…") if len(self.text) > 60 else self.text
        return (
            f"ChatResponse(model={self.model!r}, text={snippet!r}, "
            f"tokens={self.tokens_in}/{self.tokens_out}, tools={len(self.tool_calls)})"
        )
