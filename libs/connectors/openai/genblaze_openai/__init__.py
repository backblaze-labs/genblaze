"""OpenAI provider adapters for genblaze (Sora video, DALL-E image, TTS audio, chat)."""

from genblaze_openai.chat import achat, chat
from genblaze_openai.dalle import DalleProvider
from genblaze_openai.provider import SoraProvider
from genblaze_openai.tts import OpenAITTSProvider

from ._version import __version__  # noqa: F401 — re-exported

__all__ = ["SoraProvider", "DalleProvider", "OpenAITTSProvider", "chat", "achat"]
