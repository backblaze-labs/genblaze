"""NVIDIA chat-model registry — empty + permissive fallback.

NIM's chat surface (``integrate.api.nvidia.com/v1``) is OpenAI-wire-
compatible across every model NVIDIA hosts (Nemotron, Llama, Mixtral,
…). The wire shape is identical, so the SDK doesn't need pattern-keyed
families to encode per-model param differences — every chat slug uses
the same payload contract. Discovery is the upstream's job:
``GET /v1/models`` returns the authoritative live catalog and is used by
``NvidiaChatProvider.discover_models()`` for ``validate_model()``.

Multimodal *input* (image_url / video_url / audio_url content blocks) is
handled by ``NvidiaChatProvider._build_messages`` based on
``Asset.media_type``, not by any per-slug spec.
"""

from __future__ import annotations

from genblaze_core.models.enums import Modality
from genblaze_core.providers import ModelRegistry, ModelSpec

# Permissive fallback: chat surface is uniform across all NIM-hosted slugs.
_FALLBACK = ModelSpec(model_id="*", modality=Modality.TEXT)


def build_chat_registry() -> ModelRegistry:
    """Return the default chat ``ModelRegistry`` — empty, fallback-only.

    NVIDIA chat is ``DiscoverySupport.NATIVE``: ``/v1/models`` is the
    authoritative slug source. The registry holds no families because
    the param shape is uniform across the NIM chat surface.
    """
    return ModelRegistry(fallback=_FALLBACK)
