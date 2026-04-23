"""Canonical parameter names and value vocabularies.

Providers that adopt these names gain cross-provider portability: a pipeline
written with ``aspect_ratio="16:9"`` works against any provider whose
``ModelSpec.param_aliases`` maps the canonical name to its native equivalent.

Keep this vocabulary small. Add a name only if ≥3 providers need it.
"""

from __future__ import annotations

# Identity / routing
PROMPT = "prompt"
NEGATIVE_PROMPT = "negative_prompt"
SEED = "seed"
N = "n"  # number of outputs

# Inputs (chain / references)
IMAGE = "image"
IMAGE_END = "image_end"  # second image for I2V-with-endpoints
AUDIO = "audio"
VIDEO = "video"

# Output shape
DURATION = "duration"  # seconds, numeric
ASPECT_RATIO = "aspect_ratio"  # "W:H"
RESOLUTION = "resolution"  # "720p" / "1080p" / "WxH"
FPS = "fps"

# Media-specific
VOICE = "voice"
OUTPUT_FORMAT = "output_format"
QUALITY = "quality"

# Value vocabularies -------------------------------------------------------

ASPECT_RATIOS = frozenset({"1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "3:2", "2:3"})
"""Portable aspect-ratio strings. Providers alias these to their native values."""

RESOLUTIONS_TIERED = frozenset({"480p", "720p", "1080p", "1440p", "4k"})
"""Portable tiered resolutions. Providers accepting ``WxH`` alias via transformer."""


CANONICAL_NAMES = frozenset(
    {
        PROMPT,
        NEGATIVE_PROMPT,
        SEED,
        N,
        IMAGE,
        IMAGE_END,
        AUDIO,
        VIDEO,
        DURATION,
        ASPECT_RATIO,
        RESOLUTION,
        FPS,
        VOICE,
        OUTPUT_FORMAT,
        QUALITY,
    }
)
