"""Standalone OpenAI Chat Completions wrapper. Sits outside the Pipeline /
Step machinery — see ``docs/features/llm-calls.md`` for rationale and usage.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ChatResponse, ToolCall
from genblaze_core.models.enums import ProviderErrorCode

from genblaze_openai._errors import map_openai_error

# Per-1M-token rates (USD). Input, output. Cached prompt rate is 50% of input
# for OpenAI's prompt-caching tier on supported models.
# Snapshot dates trim to the family slug so dated variants reuse the same row.
_RATES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (1.10, 4.40),
}


def _lookup_rate(model: str) -> tuple[float, float] | None:
    """Resolve a rate row for `model`, stripping dated `-YYYY-MM-DD` suffixes."""
    if model in _RATES:
        return _RATES[model]
    # Strip a `-YYYY-MM-DD` snapshot suffix (e.g. gpt-4o-2024-11-20 → gpt-4o).
    parts = model.rsplit("-", 3)
    if len(parts) >= 4 and all(p.isdigit() for p in parts[1:4]):
        return _RATES.get(parts[0])
    return None


def _calc_cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    """Compute USD cost from token counts; None when model isn't in the table."""
    if tokens_in is None or tokens_out is None:
        return None
    rate = _lookup_rate(model)
    if rate is None:
        return None
    in_rate, out_rate = rate
    return (tokens_in / 1_000_000) * in_rate + (tokens_out / 1_000_000) * out_rate


def _normalize_messages(
    messages: list[ChatMessage] | list[dict] | None,
    prompt: str | None,
    system: str | None,
) -> list[dict]:
    """Build the OpenAI-shaped message list from any of the input forms."""
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
                                # OpenAI requires a JSON-encoded string, not Python repr.
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


def _parse_response(model: str, raw: Any) -> ChatResponse:
    """Translate an OpenAI ChatCompletion object into a uniform ChatResponse."""
    raw_dict = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
    choice = raw_dict.get("choices", [{}])[0]
    message = choice.get("message", {}) or {}
    usage = raw_dict.get("usage", {}) or {}

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
        model=raw_dict.get("model", model),
        finish_reason=choice.get("finish_reason"),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_cached=cached,
        tool_calls=tool_calls,
        cost_usd=_calc_cost(model, tokens_in, tokens_out),
        raw=raw_dict,
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
    timeout: float = 60.0,
    client: Any = None,
    **kwargs: Any,
) -> ChatResponse:
    """Call an OpenAI chat / completion model and return a uniform `ChatResponse`.

    Args:
        model: OpenAI model id (e.g. "gpt-4o", "gpt-4o-mini").
        messages: Conversation turns. Mutually exclusive with `prompt`.
        prompt: Single-turn shorthand — wrapped as a user message.
        system: System instruction prepended when set.
        tools: Tool/function definitions in OpenAI's native shape.
        temperature, max_tokens: Standard sampling controls (passed through if set).
        api_key: API key override; otherwise OPENAI_API_KEY env var.
        timeout: HTTP timeout in seconds.
        client: Pre-built `openai.OpenAI` instance — escape hatch for tests
            and custom clients (e.g. Azure OpenAI).
        **kwargs: Forwarded to `client.chat.completions.create`.

    Raises:
        ProviderError: With a classified `error_code` for any SDK exception.
    """
    if client is None:
        try:
            import openai
        except ImportError as exc:
            raise ProviderError("openai package not installed. Run: pip install openai") from exc
        ckwargs: dict[str, Any] = {"timeout": timeout}
        if api_key:
            ckwargs["api_key"] = api_key
        client = openai.OpenAI(**ckwargs)

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

    try:
        raw = client.chat.completions.create(**payload)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"OpenAI chat failed: {exc}",
            error_code=map_openai_error(exc),
        ) from exc

    return _parse_response(model, raw)


async def achat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Async wrapper around `chat()`. Runs the sync call in a worker thread.

    Matches the in-tree `BaseProvider.ainvoke` pattern (uses `asyncio.to_thread`
    rather than the SDK's native async client) for consistency. Switch to
    `openai.AsyncOpenAI` here if true async I/O ever shows up in profiling.
    """
    return await asyncio.to_thread(chat, model, messages, **kwargs)
