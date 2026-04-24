"""Curated NVIDIA model registries (video, image, audio).

Every registry uses a permissive fallback spec so unlisted NIM models still
work via pass-through — `build.nvidia.com` ships new models faster than we
can enumerate them, and hardcoding would create a stale-list maintenance
trap. Curated entries exist only for models where we've encoded real
per-model behavior (param shapes, constraints) that catches typos at
manifest-build time.
"""

from genblaze_nvidia.models.audio import build_audio_registry
from genblaze_nvidia.models.image import build_image_registry
from genblaze_nvidia.models.video import build_video_registry

__all__ = ["build_audio_registry", "build_image_registry", "build_video_registry"]
