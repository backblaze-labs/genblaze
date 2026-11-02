"""LiteLLM provider adapters for genblaze (image, speech, and video generation, chat)."""

from genblaze_litellm.chat import achat, chat
from genblaze_litellm.image import LiteLLMImageProvider
from genblaze_litellm.speech import LiteLLMSpeechProvider
from genblaze_litellm.video import LiteLLMVideoProvider

from ._version import __version__  # noqa: F401 — re-exported

__all__ = [
    "LiteLLMImageProvider",
    "LiteLLMSpeechProvider",
    "LiteLLMVideoProvider",
    "chat",
    "achat",
]
