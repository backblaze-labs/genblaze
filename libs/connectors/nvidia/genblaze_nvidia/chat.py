"""Standalone NVIDIA NIM chat wrapper (``integrate.api.nvidia.com/v1``).

NIM's chat surface is OpenAI-wire-compatible — we use the ``openai`` Python
SDK directly with the base URL overridden. Model ids are free-form; NIM ships
new LLMs faster than any enumeration could keep up, so nothing is hardcoded.
Pricing is ``None`` (free tier is RPM-gated, not per-token).

Install with the chat extra::

    pip install "genblaze-nvidia[chat]"
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ChatResponse, ToolCall, coerce_response_format
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.retry import retry_after_from_response

from ._base import DEFAULT_CHAT_BASE_URL, resolve_api_key
from ._errors import map_nvidia_error


def _resolve_chat_base_url(explicit: str | None) -> str:
    """Pick the chat base URL. Precedence: explicit arg → env var → default."""
    return explicit or os.environ.get("NVIDIA_CHAT_BASE_URL") or DEFAULT_CHAT_BASE_URL


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
                # NIM is OpenAI-wire-compat: str content passes through; list of
                # ContentBlock dumps to OpenAI-vision-shape dicts (image_url,
                # video_url) which NIM accepts natively.
                if isinstance(m.content, str):
                    wire_content: Any = m.content
                else:
                    wire_content = [b.model_dump(exclude_none=True) for b in m.content]
                msg: dict[str, Any] = {"role": m.role, "content": wire_content}
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
                                # OpenAI-wire requires a JSON-encoded string here.
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
        # NIM free tier is RPM-gated — no per-token cost. Enterprise pricing is
        # contract-specific; callers who need cost tracking compute it downstream.
        cost_usd=None,
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
    response_format: dict | type | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
    client: Any = None,
    **kwargs: Any,
) -> ChatResponse:
    """Call a NIM-hosted chat model and return a uniform ``ChatResponse``.

    Args:
        model: NIM model id (e.g. ``"meta/llama-3.3-70b-instruct"``,
            ``"nvidia/nemotron-4-340b-instruct"``, ``"mistralai/mixtral-8x22b-instruct-v0.1"``).
            Pass-through — any string NIM currently serves works.
        messages: Conversation turns. Mutually exclusive with ``prompt``.
        prompt: Single-turn shorthand — wrapped as a user message.
        system: System instruction prepended when set.
        tools: Tool / function definitions (OpenAI shape — NIM is wire-compatible).
        temperature, max_tokens: Sampling controls (passed through if set).
        response_format: Structured-output spec. Accepts a Pydantic ``BaseModel``
            subclass (auto-generates the JSON schema) or a pre-formed dict. NIM is
            OpenAI-wire-compatible, so the same envelope shape works.
        api_key: API key override; otherwise ``NVIDIA_API_KEY`` /
            ``NVIDIA_NIM_API_KEY`` env vars.
        base_url: Override the NIM inference base URL. Defaults to
            ``https://integrate.api.nvidia.com/v1``. Point at a self-hosted NIM.
        timeout: HTTP timeout in seconds.
        client: Pre-built ``openai.OpenAI`` instance — escape hatch for tests
            and shared clients. When set, ``api_key`` / ``base_url`` are ignored.
        **kwargs: Forwarded to ``client.chat.completions.create``.

    Raises:
        ProviderError: With a classified ``error_code`` for any SDK exception.
    """
    key = resolve_api_key(api_key)
    if not key and client is None:
        raise ProviderError(
            "No NVIDIA API key found. Set NVIDIA_API_KEY env var or pass api_key=.",
            error_code=ProviderErrorCode.AUTH_FAILURE,
        )

    own_client = client is None
    if own_client:
        try:
            import openai
        except ImportError as exc:
            raise ProviderError(
                'openai package not installed. Run: pip install "genblaze-nvidia[chat]"'
            ) from exc
        client = openai.OpenAI(
            api_key=key,
            base_url=_resolve_chat_base_url(base_url),
            timeout=timeout,
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
    if response_format is not None:
        payload["response_format"] = coerce_response_format(response_format)
    payload.update(kwargs)

    try:
        raw = client.chat.completions.create(**payload)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"NVIDIA chat failed: {exc}",
            error_code=map_nvidia_error(exc),
            retry_after=retry_after_from_response(exc),
        ) from exc
    finally:
        if own_client:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()

    return _parse_response(model, raw)


async def achat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Async wrapper around :func:`chat`. Runs the sync call in a worker thread."""
    return await asyncio.to_thread(chat, model, messages, **kwargs)
