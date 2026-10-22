"""Standalone LiteLLM chat wrapper. Sits outside the Pipeline / Step machinery —
see ``docs/features/llm-calls.md`` for rationale and usage.

One function for every chat model LiteLLM routes to (Anthropic, Bedrock,
Vertex AI, Mistral, Groq, Ollama, ...), returning the same ``ChatResponse``
as the OpenAI, Google, GMICloud, and NVIDIA helpers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ChatResponse, ToolCall, coerce_response_format
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.retry import (
    RetryPolicy,
    call_with_rate_limit_retry,
    retry_after_from_response,
)

from genblaze_litellm._common import credential_kwargs, import_litellm, response_cost
from genblaze_litellm._errors import map_litellm_error


def _normalize_messages(
    messages: list[ChatMessage] | list[dict] | None,
    prompt: str | None,
    system: str | None,
) -> list[dict]:
    """Build the OpenAI-shaped message list LiteLLM accepts for every provider."""
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
    """Translate a LiteLLM ``ModelResponse`` into a uniform ``ChatResponse``."""
    raw_dict = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
    choice = (raw_dict.get("choices") or [{}])[0]
    message = choice.get("message", {}) or {}
    usage = raw_dict.get("usage", {}) or {}

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
        model=raw_dict.get("model") or model,
        finish_reason=choice.get("finish_reason"),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        tokens_cached=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        tool_calls=tool_calls,
        # LiteLLM prices the call from its model map; None when it has no price.
        cost_usd=response_cost(raw),
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
    api_base: str | None = None,
    timeout: float = 60.0,
    retry_on_rate_limit: bool = False,
    retry_policy: RetryPolicy | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Call any LiteLLM chat model and return a uniform ``ChatResponse``.

    Args:
        model: LiteLLM model id (e.g. ``"anthropic/claude-sonnet-4-6"``,
            ``"bedrock/us.anthropic.claude-sonnet-4-6-v1:0"``,
            ``"vertex_ai/gemini-2.5-pro"``, ``"ollama/llama3.1"``).
        messages: Conversation turns. Mutually exclusive with ``prompt``.
        prompt: Single-turn shorthand — wrapped as a user message.
        system: System instruction prepended when set.
        tools: Tool/function definitions in OpenAI's shape; LiteLLM translates
            them for each provider.
        temperature, max_tokens: Standard sampling controls (passed through if set).
        response_format: Structured-output spec. Accepts a Pydantic ``BaseModel``
            subclass or a pre-formed dict.
        api_key: Provider key override; otherwise the provider's own env var.
        api_base: Custom endpoint (Azure deployment, self-hosted server).
        timeout: HTTP timeout in seconds.
        retry_on_rate_limit: When ``True``, waits and retries 429 and 5xx using
            the server's ``Retry-After`` hint. Off by default.
        retry_policy: Optional ``RetryPolicy`` for the retry loop (passing it
            alone opts in).
        **kwargs: Forwarded to ``litellm.completion``.

    Unsupported params for a given provider are dropped (``drop_params=True``)
    rather than failing the call. ``ChatResponse.cost_usd`` carries the cost
    LiteLLM computed for the call, or ``None`` when it has no price.

    Raises:
        ProviderError: With a classified ``error_code`` for any LiteLLM exception.
    """
    litellm = import_litellm()
    retry_managed = retry_on_rate_limit or retry_policy is not None

    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages, prompt, system),
        "timeout": timeout,
        "drop_params": True,
        **credential_kwargs(api_key, api_base),
    }
    if tools is not None:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format is not None:
        payload["response_format"] = coerce_response_format(response_format)
    if retry_managed:
        # call_with_rate_limit_retry owns retries; keep the SDK's own retry
        # layer from multiplying RetryPolicy.max_attempts.
        payload["max_retries"] = 0
    payload.update(kwargs)

    def _invoke() -> Any:
        try:
            return litellm.completion(**payload)
        except Exception as exc:
            raise ProviderError(
                f"LiteLLM chat failed: {exc}",
                error_code=map_litellm_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    raw = call_with_rate_limit_retry(_invoke, policy=retry_policy) if retry_managed else _invoke()
    return _parse_response(model, raw)


async def achat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Async wrapper around ``chat()``. Runs the sync call in a worker thread.

    Matches the other in-tree chat helpers (``asyncio.to_thread``), so retry
    backoff never blocks the event loop.
    """
    return await asyncio.to_thread(chat, model, messages, **kwargs)
