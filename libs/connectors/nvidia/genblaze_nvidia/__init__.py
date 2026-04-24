"""NVIDIA NIM provider adapters for genblaze (video, image, audio, chat).

All four modalities share a single ``nvapi-`` key (``NVIDIA_API_KEY`` env var)
and a common HTTP layer. The three *Provider classes integrate with Pipelines
via entry points; the ``chat`` / ``achat`` helpers sit alongside for LLM calls.
"""

from genblaze_nvidia.audio import NvidiaAudioProvider
from genblaze_nvidia.chat import achat, chat
from genblaze_nvidia.image import NvidiaImageProvider
from genblaze_nvidia.video import NvidiaVideoProvider

__all__ = [
    "NvidiaVideoProvider",
    "NvidiaImageProvider",
    "NvidiaAudioProvider",
    "chat",
    "achat",
]
