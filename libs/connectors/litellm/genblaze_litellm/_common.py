"""Shared LiteLLM plumbing for the image, speech, video, and chat adapters."""

from __future__ import annotations

import logging
import os
from typing import Any

from genblaze_core.exceptions import ProviderError


def import_litellm() -> Any:
    """Import ``litellm`` with its console chatter turned down.

    LiteLLM logs every call at INFO and prints a "Give Feedback" banner on
    errors; genblaze reports failures through ``ProviderError`` instead.
    Set ``LITELLM_LOG`` to keep LiteLLM's own logging for debugging.
    """
    try:
        import litellm
    except ImportError as exc:
        raise ProviderError(
            "litellm package not installed. Run: pip install genblaze-litellm"
        ) from exc
    if not os.getenv("LITELLM_LOG"):
        litellm.suppress_debug_info = True
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    return litellm


def credential_kwargs(api_key: str | None, api_base: str | None) -> dict[str, Any]:
    """Credentials for a LiteLLM call.

    Blank values are omitted so LiteLLM reads the provider's own env vars
    (``OPENAI_API_KEY``, AWS credentials for ``bedrock/``,
    ``GOOGLE_APPLICATION_CREDENTIALS`` for ``vertex_ai/`` ...).
    """
    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def response_cost(response: Any) -> float | None:
    """Cost LiteLLM computed for this response from its model price map.

    ``None`` when LiteLLM has no price for the model, so genblaze's own
    ``register_pricing`` recipes still apply.
    """
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, dict):
        return None
    try:
        value = hidden.get("response_cost")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def jsonable(value: Any) -> Any:
    """Best-effort conversion of a LiteLLM usage object to plain JSON types."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return str(value)
