"""Voice — describes a single TTS/music voice option exposed by an audio provider.

Returned from ``AudioProvider.list_voices()`` (a ``BaseProvider`` hook) so apps
can build voice pickers without hardcoding opaque strings. Catalogs are either
fetched live (ElevenLabs, LMNT) or curated in the connector (GMI, NVIDIA Riva,
OpenAI TTS) — the contract is the same either way.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VoiceGender = Literal["male", "female", "neutral"]


class Voice(BaseModel):
    """A single voice exposed by an audio provider.

    Most fields are optional because upstream catalogs vary in detail. Apps
    that need stable filtering should rely on ``voice_id`` (always present)
    and ``provider`` (always present); everything else is best-effort metadata.
    """

    voice_id: str = Field(description="Provider-native id passed back as `voice_id` param.")
    name: str = Field(description="Human-readable display name.")
    provider: str = Field(description="Provider name (matches ``BaseProvider.name``).")
    model: str | None = Field(
        default=None,
        description=(
            "Specific model the voice belongs to. ``None`` means the voice works"
            " across every model the provider exposes."
        ),
    )
    gender: VoiceGender | None = Field(default=None)
    language: str | None = Field(default=None, description="BCP 47 tag (e.g. 'en-US', 'es-MX').")
    style_tags: tuple[str, ...] = Field(
        default=(),
        description="Free-form descriptors ('warm', 'narration', 'announcer').",
    )
    sample_url: str | None = Field(
        default=None, description="HTTPS URL of a short preview clip if the upstream supplies one."
    )
    deprecated: bool = Field(
        default=False,
        description="True if the voice is still resolvable but slated for removal upstream.",
    )

    model_config = ConfigDict(frozen=True, extra="forbid")
