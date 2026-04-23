"""GMICloud model registries — one per modality."""

from genblaze_gmicloud.models.audio import build_audio_registry
from genblaze_gmicloud.models.image import build_image_registry
from genblaze_gmicloud.models.video import build_video_registry

__all__ = ["build_audio_registry", "build_image_registry", "build_video_registry"]
