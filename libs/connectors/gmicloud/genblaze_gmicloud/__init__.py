"""GMICloud provider adapters for genblaze (video, image, audio, chat)."""

from genblaze_gmicloud.audio import GMICloudAudioProvider
from genblaze_gmicloud.chat import achat, chat
from genblaze_gmicloud.image import GMICloudImageProvider
from genblaze_gmicloud.provider import GMICloudVideoProvider

from ._version import __version__  # noqa: F401 — re-exported

__all__ = [
    "GMICloudVideoProvider",
    "GMICloudImageProvider",
    "GMICloudAudioProvider",
    "chat",
    "achat",
]
