"""Shared chat-call models — used by `chat()` callables in connector packages.

These types are *not* part of the manifest wire protocol. They are function
return types for the standalone `chat()` / `achat()` entry points exposed by
`genblaze_openai`, `genblaze_google`, and `genblaze_gmicloud`. Keeping the
shape uniform across connectors lets callers swap providers without rewriting
response-handling code.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ChatRole = Literal["system", "user", "assistant", "tool"]


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
    """Canonical chat turn. Connectors translate to / from native shapes."""

    role: ChatRole
    content: str = Field(
        default="", description="Text content. Empty for assistant tool-call turns."
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
