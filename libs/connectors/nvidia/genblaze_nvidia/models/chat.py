"""NVIDIA chat model specs (Nemotron + Llama families on NIM).

Pricing is ``None`` for all entries: ``integrate.api.nvidia.com`` free tier
is RPM-gated (no per-token billing) and enterprise NIM pricing is contract-
specific. Token counts ARE always populated on the response, so callers who
track cost downstream compute ``tokens × negotiated_rate``. Unlisted models
pass through the fallback spec.

Modality is ``Modality.TEXT`` because chat outputs text. Multimodal *input*
is handled via ``Asset.media_type`` on ``step.inputs`` — see
``NvidiaChatProvider._build_messages``.
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import ModelRegistry, ModelSpec

# Nemotron 3 multimodal / Omni / VL families. Multimodal accepts image_url
# and video_url content blocks (NIM is OpenAI-vision-wire-compat). Document
# inputs are not natively supported — callers rasterize PDFs to images.
_NEMOTRON_MULTIMODAL: tuple[str, ...] = (
    "nvidia/nemotron-3-nano-omni",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-nano-12b-v2-vl",
)

# Text-only Nemotron / Llama variants on NIM. Pass-through is fine here too,
# but listing them lets `get_capabilities().models` advertise the curated set.
_TEXT_ONLY: tuple[str, ...] = (
    "nvidia/nemotron-4-340b-instruct",
    "nvidia/nemotron-3-nano-30b-a3b",
    "meta/llama-3.3-70b-instruct",
    "mistralai/mixtral-8x22b-instruct-v0.1",
)


def _multimodal_spec(model_id: str) -> ModelSpec:
    return ModelSpec(model_id=model_id, modality=Modality.TEXT, pricing=None)


def _text_only_spec(model_id: str) -> ModelSpec:
    return ModelSpec(model_id=model_id, modality=Modality.TEXT, pricing=None)


_FALLBACK = ModelSpec(model_id="*", modality=Modality.TEXT)


def build_chat_registry() -> ModelRegistry:
    """Return the default chat ModelRegistry. Pass-through for unknown ids."""
    defaults: dict[str, ModelSpec] = {}
    for mid in _NEMOTRON_MULTIMODAL:
        defaults[mid] = _multimodal_spec(mid)
    for mid in _TEXT_ONLY:
        defaults[mid] = _text_only_spec(mid)
    return ModelRegistry(defaults=defaults, fallback=_FALLBACK)
