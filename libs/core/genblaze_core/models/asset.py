"""Asset model — a generated media artifact."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from genblaze_core._utils import compute_sha256, new_id


class WordTiming(BaseModel):
    """A single word with its time boundaries in the audio."""

    word: str = Field(description="The spoken word or token.")
    start: float = Field(description="Start time in seconds.")
    end: float = Field(description="End time in seconds.")
    confidence: float | None = Field(default=None, description="Recognition confidence 0-1.")


class VideoMetadata(BaseModel):
    """Technical metadata for video assets (codec, frame rate, etc.)."""

    frame_rate: float | None = Field(default=None, description="Frames per second.")
    codec: str | None = Field(default=None, description="Video codec (e.g. 'h264', 'vp9').")
    bitrate: int | None = Field(default=None, description="Bitrate in bits per second.")
    color_space: str | None = Field(default=None, description="Color space (e.g. 'bt709').")
    has_audio: bool | None = Field(
        default=None, description="Whether the video contains an audio track."
    )
    resolution: str | None = Field(
        default=None, description="Resolution label (e.g. '1080p', '4k')."
    )


class AudioMetadata(BaseModel):
    """Technical metadata for audio assets (sample rate, codec, etc.)."""

    sample_rate: int | None = Field(default=None, description="Sample rate in Hz (e.g. 44100).")
    channels: int | None = Field(
        default=None, description="Number of channels (1=mono, 2=stereo)."
    )
    codec: str | None = Field(default=None, description="Audio codec (e.g. 'mp3', 'aac', 'pcm').")
    bitrate: int | None = Field(default=None, description="Bitrate in bits per second.")
    word_timings: list[WordTiming] | None = Field(
        default=None, description="Word-level timing data [{word, start, end}, ...]."
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_word_timings(cls, data: Any) -> Any:
        """Convert raw dicts in word_timings to WordTiming for backward compat."""
        if isinstance(data, dict):
            wt = data.get("word_timings")
            if isinstance(wt, list):
                data["word_timings"] = [
                    item if isinstance(item, WordTiming) else WordTiming(**item) for item in wt
                ]
        return data


class Track(BaseModel):
    """Describes a media track within a container asset (e.g., video+audio in MP4)."""

    kind: str = Field(description="Track type: 'video', 'audio', 'subtitle'.")
    codec: str | None = Field(default=None, description="Track codec (e.g. 'h264', 'aac').")
    label: str | None = Field(
        default=None, description="Human-readable label (e.g. 'generated-audio')."
    )


class Asset(BaseModel):
    """A generated media artifact with URL, MIME type, and optional hash."""

    asset_id: str = Field(default_factory=new_id, description="Unique asset identifier (UUID).")
    url: str = Field(description="URL of the generated asset.")
    media_type: str = Field(description="MIME type (e.g. 'image/png').")
    sha256: str | None = Field(default=None, description="SHA-256 hash of asset content.")
    size_bytes: int | None = Field(default=None, description="File size in bytes.")
    width: int | None = Field(default=None, description="Image/video width in pixels.")
    height: int | None = Field(default=None, description="Image/video height in pixels.")
    duration: float | None = Field(default=None, description="Audio/video duration in seconds.")
    video: VideoMetadata | None = Field(
        default=None, description="Video-specific technical metadata."
    )
    audio: AudioMetadata | None = Field(
        default=None, description="Audio-specific technical metadata."
    )
    tracks: list[Track] | None = Field(
        default=None, description="Media tracks in this container asset."
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata.")

    def set_hash(self, data: bytes) -> None:
        """Compute and set sha256 + size_bytes from raw asset bytes."""
        self.sha256 = compute_sha256(data)
        self.size_bytes = len(data)

    def __repr__(self) -> str:
        return f"Asset(id={self.asset_id[:8]}..., url={self.url!r}, type={self.media_type})"
