"""Standalone GMICloud chat wrapper over the OpenAI-compatible inference
endpoint. Pricing intentionally omitted — fleet shifts faster than a static
table tracks. Auth: ``GMI_API_KEY`` env var or ``api_key=``.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ChatResponse, ToolCall
from genblaze_core.models.enums import ProviderErrorCode

from genblaze_gmicloud._base import unwrap_error_body
from genblaze_gmicloud._errors import map_gmicloud_error

# GMICloud's OpenAI-compatible inference endpoint. Override per-call with
# `base_url=` if your account uses a tenant-specific URL.
_DEFAULT_BASE_URL = "https://api.gmi-serving.com/v1"


def _normalize_messages(
    messages: list[ChatMessage] | list[dict] | None,
    prompt: str | None,
    system: str | None,
) -> list[dict]:
    """Build the OpenAI-shaped message list (GMICloud is wire-compatible)."""
    if messages is None and prompt is None:
        raise ProviderError(
            "chat() requires either `messages` or `prompt`",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )

    out: list[dict] = []
    if system is not None:
        out.append({"role": "system", "content": system})

    if messages is not None:
        for m in messages:
            if isinstance(m, ChatMessage):
                msg: dict[str, Any] = {"role": m.role, "content": m.content}
                if m.name is not None:
                    msg["name"] = m.name
                if m.tool_call_id is not None:
                    msg["tool_call_id"] = m.tool_call_id
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                # OpenAI-compatible wire requires JSON-encoded string.
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            else:
                out.append(dict(m))
    else:
        out.append({"role": "user", "content": prompt})

    return out


def _parse_response(model: str, body: dict) -> ChatResponse:
    """Parse GMICloud's OpenAI-shaped response body."""
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {}) or {}
    usage = body.get("usage", {}) or {}

    tokens_in = usage.get("prompt_tokens")
    tokens_out = usage.get("completion_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")

    tool_calls: list[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        tool_calls.append(
            ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=fn.get("arguments", {}),
            )
        )

    return ChatResponse(
        text=message.get("content") or "",
        model=body.get("model", model),
        finish_reason=choice.get("finish_reason"),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_cached=cached,
        tool_calls=tool_calls,
        cost_usd=None,  # GMICloud fleet shifts; let callers compute when needed.
        raw=body,
    )


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
    base_url: str | None = None,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Call a GMICloud-hosted chat / completion model and return a uniform `ChatResponse`.

    Args:
        model: GMICloud model id (e.g. "deepseek-ai/DeepSeek-V3",
            "meta-llama/Llama-3.3-70B-Instruct").
        messages: Conversation turns. Mutually exclusive with `prompt`.
        prompt: Single-turn shorthand — wrapped as a user message.
        system: System instruction prepended when set.
        tools: Tool definitions (OpenAI shape — GMICloud is wire-compatible).
        temperature, max_tokens: Sampling controls (passed through if set).
        api_key: API key override; otherwise GMI_API_KEY env var.
        base_url: Override the inference endpoint base URL.
        timeout: HTTP timeout in seconds.
        client: Pre-built `httpx.Client` — escape hatch for tests.
        **kwargs: Forwarded into the request body alongside the OpenAI-shaped fields.

    Raises:
        ProviderError: With a classified `error_code` for any HTTP / transport failure.
    """
    key = api_key or os.environ.get("GMI_API_KEY")
    if not key and client is None:
        raise ProviderError(
            "No API key found. Set GMI_API_KEY env var or pass api_key=.",
            error_code=ProviderErrorCode.AUTH_FAILURE,
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages, prompt, system),
    }
    if tools is not None:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    payload.update(kwargs)

    own_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=base_url or _DEFAULT_BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    try:
        resp = client.post("/chat/completions", json=payload)
        if resp.status_code >= 400:
            inner = unwrap_error_body(resp.text)
            raise ProviderError(
                f"GMICloud chat failed ({resp.status_code}): {inner}",
                error_code=map_gmicloud_error(Exception(inner), resp.status_code),
            )
        body = resp.json()
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"GMICloud chat failed: {exc}",
            error_code=map_gmicloud_error(exc),
        ) from exc
    finally:
        if own_client:
            client.close()

    return _parse_response(model, body)


async def achat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Async wrapper around `chat()`. Runs in a worker thread."""
    return await asyncio.to_thread(chat, model, messages, **kwargs)
