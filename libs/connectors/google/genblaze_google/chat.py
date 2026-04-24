"""Standalone Google Gemini chat wrapper. Mirrors ``genblaze_openai.chat`` —
same signature, same ``ChatResponse`` shape. Auth: ``GEMINI_API_KEY`` or
``project=`` for Vertex AI.
"""

from __future__ import annotations

import asyncio
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ChatResponse, ToolCall
from genblaze_core.models.enums import ProviderErrorCode

from genblaze_google._errors import map_google_error

# Per-1M-token rates (USD). Input, output. Tiered models (1.5-pro, 2.5-pro)
# use the small-context rate by default; large-context surcharges aren't
# applied without inspecting prompt length, which we leave to the caller.
_RATES: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-flash-8b": (0.0375, 0.15),
}


def _lookup_rate(model: str) -> tuple[float, float] | None:
    """Resolve rate row, ignoring trailing version suffix like `-001` or `-latest`."""
    if model in _RATES:
        return _RATES[model]
    # Strip a trailing `-NNN` or `-latest` so dated/versioned slugs reuse the family row.
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and (parts[1].isdigit() or parts[1] in ("latest", "exp")):
        return _RATES.get(parts[0])
    return None


def _calc_cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    if tokens_in is None or tokens_out is None:
        return None
    rate = _lookup_rate(model)
    if rate is None:
        return None
    in_rate, out_rate = rate
    return (tokens_in / 1_000_000) * in_rate + (tokens_out / 1_000_000) * out_rate


# Gemini uses "model" / "user" roles; "assistant" maps to "model".
_ROLE_MAP = {"user": "user", "assistant": "model", "system": "user", "tool": "user"}


def _normalize_to_gemini(
    messages: list[ChatMessage] | list[dict] | None,
    prompt: str | None,
    system: str | None,
) -> tuple[list[dict], str | None]:
    """Translate canonical messages to Gemini's `contents` + extracted system instruction."""
    if messages is None and prompt is None:
        raise ProviderError(
            "chat() requires either `messages` or `prompt`",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )

    system_instruction = system
    contents: list[dict] = []

    iter_msgs: list[Any] = list(messages) if messages is not None else []
    if not iter_msgs and prompt is not None:
        iter_msgs = [ChatMessage(role="user", content=prompt)]

    for m in iter_msgs:
        msg = m if isinstance(m, ChatMessage) else ChatMessage(**m)
        # Gemini takes a separate system_instruction; pull the first system msg out.
        if msg.role == "system":
            if system_instruction is None:
                system_instruction = msg.content
            continue
        role = _ROLE_MAP.get(msg.role, "user")
        contents.append({"role": role, "parts": [{"text": msg.content}]})

    return contents, system_instruction


def _parse_response(model: str, raw: Any) -> ChatResponse:
    """Translate a Gemini GenerateContentResponse into a uniform ChatResponse."""
    raw_dict = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)

    text = ""
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = []

    candidates = raw_dict.get("candidates") or []
    if candidates:
        first = candidates[0] or {}
        # Real SDK returns FinishReason enum; stringify so the str field validates.
        fr = first.get("finish_reason")
        finish_reason = str(fr).rsplit(".", 1)[-1] if fr is not None else None
        for part in (first.get("content") or {}).get("parts") or []:
            if "text" in part and part["text"]:
                text += part["text"]
            fn = part.get("function_call")
            if fn:
                tool_calls.append(
                    ToolCall(
                        id=fn.get("id", fn.get("name", "")),
                        name=fn.get("name", ""),
                        arguments=fn.get("args", {}) or {},
                    )
                )

    usage = raw_dict.get("usage_metadata") or {}
    tokens_in = usage.get("prompt_token_count")
    tokens_out = usage.get("candidates_token_count")
    cached = usage.get("cached_content_token_count")

    return ChatResponse(
        text=text,
        model=raw_dict.get("model_version") or model,
        finish_reason=finish_reason,
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
    project: str | None = None,
    location: str = "us-central1",
    client: Any = None,
    **kwargs: Any,
) -> ChatResponse:
    """Call a Google Gemini model and return a uniform `ChatResponse`.

    Args:
        model: Gemini model id (e.g. "gemini-2.5-flash", "gemini-2.5-pro").
        messages: Conversation turns. Mutually exclusive with `prompt`.
        prompt: Single-turn shorthand — wrapped as a user message.
        system: System instruction (Gemini's `system_instruction`).
        tools: Tool definitions in Gemini's native shape (function_declarations).
        temperature, max_tokens: Sampling controls (mapped to `generation_config`).
        api_key: Gemini API key override; otherwise GEMINI_API_KEY env var.
        project: GCP project for Vertex AI auth (mutually exclusive with api_key).
        location: GCP region for Vertex AI.
        client: Pre-built `google.genai.Client` — escape hatch for tests.
        **kwargs: Extra keys merged into the `generation_config`.

    Raises:
        ProviderError: With a classified `error_code` for any SDK exception.
    """
    own_client = client is None
    if own_client:
        try:
            from google import genai
        except ImportError as exc:
            raise ProviderError(
                "google-genai package not installed. Run: pip install google-genai"
            ) from exc
        if project:
            client = genai.Client(vertexai=True, project=project, location=location)
        else:
            ckwargs: dict[str, Any] = {}
            if api_key:
                ckwargs["api_key"] = api_key
            client = genai.Client(**ckwargs)

    contents, system_instruction = _normalize_to_gemini(messages, prompt, system)

    gen_config: dict[str, Any] = {}
    if temperature is not None:
        gen_config["temperature"] = temperature
    if max_tokens is not None:
        gen_config["max_output_tokens"] = max_tokens
    if system_instruction is not None:
        gen_config["system_instruction"] = system_instruction
    if tools is not None:
        gen_config["tools"] = tools
    gen_config.update(kwargs)

    call_kwargs: dict[str, Any] = {"model": model, "contents": contents}
    if gen_config:
        call_kwargs["config"] = gen_config

    try:
        raw = client.models.generate_content(**call_kwargs)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"Gemini chat failed: {exc}",
            error_code=map_google_error(exc),
        ) from exc
    finally:
        if own_client:
            # Best-effort close — not all google-genai versions expose close(),
            # so probe via hasattr to stay compatible.
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()

    return _parse_response(model, raw)


async def achat(
    model: str,
    messages: list[ChatMessage] | list[dict] | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """Async wrapper around `chat()`. Runs in a worker thread (matches `BaseProvider.ainvoke`)."""
    return await asyncio.to_thread(chat, model, messages, **kwargs)
