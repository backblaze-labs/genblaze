"""NvidiaChatProvider — NIM chat as a Pipeline step.

Wraps NVIDIA's OpenAI-wire-compatible chat completions surface as a
``SyncProvider`` so callers can compose chat into Pipelines alongside image,
video, and audio generation. Multimodal input flows through ``step.inputs``:
each ``Asset`` becomes an OpenAI-vision-shape content block based on its
``media_type``.

Why a direct ``SyncProvider`` subclass and not a generic ``ChatProvider``
base: there's only one concrete chat-as-Pipeline-step provider today
(NVIDIA). When a second one ships (Whisper, Gemini chat), extracting a base
class is cheap; building one for a single consumer is premature.
"""

from __future__ import annotations

import hashlib
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.chat import (
    AudioURLContent,
    AudioURLRef,
    ChatMessage,
    ImageURLContent,
    ImageURLRef,
    TextContent,
    VideoURLContent,
    VideoURLRef,
    coerce_response_format,
)
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.retry import RetryPolicy, retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._base import resolve_api_key
from ._errors import map_nvidia_error
from .chat import _resolve_chat_base_url, _normalize_messages, _parse_response
from .models.chat import build_chat_registry


class NvidiaChatProvider(SyncProvider):
    """Adapter for NVIDIA NIM chat completions (Nemotron, Llama, Mixtral on NIM).

    Multimodal use: pass user-uploaded images/video as ``step.inputs[Asset]``
    with the correct ``media_type``. The provider builds the OpenAI-vision-shape
    ``messages[].content[]`` array automatically. PDFs are not natively
    supported — callers must rasterize pages to images first (Nemotron 3 Nano
    Omni processes documents as multi-page image sequences upstream).

    Args:
        api_key: NVIDIA API key. Falls back to ``NVIDIA_API_KEY`` /
            ``NVIDIA_NIM_API_KEY`` env vars.
        base_url: Override the chat base URL. Falls back to
            ``NVIDIA_CHAT_BASE_URL`` env var; defaults to
            ``https://integrate.api.nvidia.com/v1``.
        timeout: HTTP timeout in seconds.
        reasoning: Tri-state thinking-mode toggle. ``None`` (default) does
            not send the kwarg — NIM picks based on the model checkpoint
            (reasoning-suffixed defaults thinking-on; base defaults
            thinking-off). ``True``/``False`` overrides explicitly via
            ``extra_body["chat_template_kwargs"]["enable_thinking"]``.
        media_io_kwargs: NIM-specific media I/O tuning passed via
            ``extra_body`` (e.g. ``{"video": {"fps": 3.0}}`` or
            ``{"video": {"num_frames": 16}}``).
        mm_processor_kwargs: NIM image-tiling controls via ``extra_body``
            (e.g. ``{"max_num_tiles": 3}``).
        client: Pre-built ``openai.OpenAI`` instance — escape hatch for tests
            and shared clients. When set, ``api_key`` / ``base_url`` are
            ignored.
        models: Optional custom ``ModelRegistry``.
        retry_policy: Optional retry policy override.
    """

    name = "nvidia-chat"

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return build_chat_registry()

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        reasoning: bool | None = None,
        media_io_kwargs: dict[str, Any] | None = None,
        mm_processor_kwargs: dict[str, Any] | None = None,
        client: Any = None,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(models=models, retry_policy=retry_policy)
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._reasoning = reasoning
        self._media_io_kwargs = media_io_kwargs
        self._mm_processor_kwargs = mm_processor_kwargs
        self._injected_client = client

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.TEXT],
            # NIM Nemotron Omni accepts image_url, video_url, audio_url content
            # blocks (verified against the build.nvidia.com / HuggingFace model
            # card). Documents are not native — PDFs flow as pre-rasterized
            # images via media_type="image/*".
            supported_inputs=["text", "image", "video", "audio"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["text/plain"],
        )

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        client = self._resolve_client()
        own_client = client is not self._injected_client

        try:
            messages = self._build_messages(step)
            payload = self._build_payload(step, messages)
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

            response = _parse_response(step.model, raw)
        finally:
            if own_client:
                close_fn = getattr(client, "close", None)
                if callable(close_fn):
                    close_fn()

        text = response.text or ""
        # Output asset carries the chat text. Asset.text (active plan Wave 4) is
        # the natural home; until it ships, populate metadata['text'] alongside
        # so generic consumers can still find the payload. Sha256 over text bytes
        # gives stable cache keys even before Wave 4 lands.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        asset_kwargs: dict[str, Any] = {
            "url": f"text:{digest}",  # synthetic; replaced by real Asset.text post-Wave 4
            "media_type": "text/plain",
            "sha256": digest,
            "size_bytes": len(text.encode("utf-8")),
            "metadata": {"text": text},
        }
        step.assets = [Asset(**asset_kwargs)]
        # Free tier is RPM-gated; cost is contract-specific or None. Token
        # counts always populated for downstream cost-tracking.
        step.cost_usd = None
        step.provider_payload["usage"] = {
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "tokens_cached": response.tokens_cached,
        }
        if response.finish_reason is not None:
            step.provider_payload["finish_reason"] = response.finish_reason
        return step

    # --- helpers ---

    def _resolve_client(self) -> Any:
        """Return injected client or lazy-construct an openai.OpenAI."""
        if self._injected_client is not None:
            return self._injected_client
        key = resolve_api_key(self._api_key)
        if not key:
            raise ProviderError(
                "No NVIDIA API key found. Set NVIDIA_API_KEY env var or pass api_key=.",
                error_code=ProviderErrorCode.AUTH_FAILURE,
            )
        try:
            import openai
        except ImportError as exc:
            raise ProviderError(
                'openai package not installed. Run: pip install "genblaze-nvidia[chat]"',
            ) from exc
        return openai.OpenAI(
            api_key=key,
            base_url=_resolve_chat_base_url(self._base_url),
            timeout=self._timeout,
        )

    def _build_messages(self, step: Step) -> list[ChatMessage]:
        """Compose chat messages from step.prompt + step.inputs[Asset].

        Single-turn shape: one user message whose content is the prompt text
        plus a content block per input asset. Empty inputs collapse to plain
        string content for the cheaper wire shape.
        """
        if not step.prompt and not step.inputs:
            raise ProviderError(
                "NvidiaChatProvider requires step.prompt or at least one step.inputs asset",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )

        if not step.inputs:
            return [ChatMessage(role="user", content=step.prompt or "")]

        blocks: list[Any] = []
        if step.prompt:
            blocks.append(TextContent(text=step.prompt))
        for asset in step.inputs:
            blocks.append(_asset_to_block(asset))
        return [ChatMessage(role="user", content=blocks)]

    def _build_payload(self, step: Step, messages: list[ChatMessage]) -> dict[str, Any]:
        """Build the chat.completions.create kwargs from the step + provider config."""
        payload: dict[str, Any] = {
            "model": step.model,
            "messages": _normalize_messages(messages, prompt=None, system=None),
        }
        # Step.params is the user-tunable surface — pass through whatever the
        # caller set (temperature, max_tokens, response_format, tools, etc.).
        params = dict(step.params or {})
        rf = params.pop("response_format", None)
        if rf is not None:
            payload["response_format"] = coerce_response_format(rf)
        payload.update(params)

        extra_body: dict[str, Any] = dict(payload.pop("extra_body", {}) or {})
        if self._reasoning is not None:
            ctk = dict(extra_body.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = self._reasoning
            extra_body["chat_template_kwargs"] = ctk
        if self._media_io_kwargs is not None:
            extra_body["media_io_kwargs"] = self._media_io_kwargs
        if self._mm_processor_kwargs is not None:
            extra_body["mm_processor_kwargs"] = self._mm_processor_kwargs
        if extra_body:
            payload["extra_body"] = extra_body
        return payload


def _asset_to_block(asset: Asset) -> Any:
    """Map an Asset to the right OpenAI-vision content block by media_type.

    Verified against the Nemotron 3 Nano Omni model card on build.nvidia.com:
    image_url / video_url / audio_url block shapes are all accepted. PDFs raise
    — Nemotron Omni processes them as multi-page image sequences upstream, so
    the caller must rasterize first.
    """
    mt = (asset.media_type or "").lower()
    if mt.startswith("image/"):
        return ImageURLContent(image_url=ImageURLRef(url=asset.url, media_type=mt))
    if mt.startswith("video/"):
        return VideoURLContent(video_url=VideoURLRef(url=asset.url, media_type=mt))
    if mt.startswith("audio/"):
        return AudioURLContent(audio_url=AudioURLRef(url=asset.url, media_type=mt))
    if mt == "application/pdf":
        raise ProviderError(
            "PDF input is not natively supported by NIM chat. Rasterize pages "
            "to images and pass each as Asset(media_type='image/png'). See "
            "the genblaze-nvidia README for the recipe.",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )
    raise ProviderError(
        f"Unsupported input media_type for NIM chat: {asset.media_type!r}. "
        "Supported: image/*, video/*, audio/*.",
        error_code=ProviderErrorCode.INVALID_INPUT,
    )
