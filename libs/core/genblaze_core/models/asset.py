"""Asset model — a generated media artifact."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from genblaze_core._utils import compute_sha256, new_id

_SHA256_HEX_CHARS = frozenset("0123456789abcdef")

# Loose MIME "type/subtype" shape (e.g. "image/png", "application/octet-stream").
# Intentionally permissive on subtype characters (RFC 6838 allows '+', '-', '.')
# since this is a sanity check against garbage, not a full MIME validator.
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.+-]*$")


def is_valid_sha256(value: str | None) -> bool:
    """Return True for syntactically valid lowercase SHA-256 hex digests."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _SHA256_HEX_CHARS for char in value)
    )


class WordTiming(BaseModel):
    """A single word with its time boundaries in the audio."""

    word: str = Field(description="The spoken word or token.")
    start: float = Field(ge=0, allow_inf_nan=False, description="Start time in seconds.")
    end: float = Field(ge=0, allow_inf_nan=False, description="End time in seconds.")
    confidence: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False, description="Recognition confidence 0-1."
    )

    @model_validator(mode="after")
    def _validate_end_after_start(self) -> WordTiming:
        if self.end < self.start:
            raise ValueError(f"WordTiming.end ({self.end}) must be >= start ({self.start})")
        return self


class VideoMetadata(BaseModel):
    """Technical metadata for video assets (codec, frame rate, etc.)."""

    frame_rate: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, description="Frames per second."
    )
    codec: str | None = Field(default=None, description="Video codec (e.g. 'h264', 'vp9').")
    bitrate: int | None = Field(default=None, gt=0, description="Bitrate in bits per second.")
    color_space: str | None = Field(default=None, description="Color space (e.g. 'bt709').")
    has_audio: bool | None = Field(
        default=None, description="Whether the video contains an audio track."
    )
    resolution: str | None = Field(
        default=None, description="Resolution label (e.g. '1080p', '4k')."
    )


class AudioMetadata(BaseModel):
    """Technical metadata for audio assets (sample rate, codec, etc.)."""

    sample_rate: int | None = Field(
        default=None, gt=0, description="Sample rate in Hz (e.g. 44100)."
    )
    channels: int | None = Field(
        default=None, gt=0, description="Number of channels (1=mono, 2=stereo)."
    )
    codec: str | None = Field(default=None, description="Audio codec (e.g. 'mp3', 'aac', 'pcm').")
    bitrate: int | None = Field(default=None, gt=0, description="Bitrate in bits per second.")
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
    url: str = Field(
        description=(
            "Durable, credential-free URL of the generated asset. After "
            "ObjectStorageSink uploads, this is rewritten to the backend's "
            "durable URL — never a presigned URL. There is no separate "
            "storage-key field; parse the key from this URL if the sink "
            "backend is known. For fetchable short-lived access, call "
            "the backend's get_url() directly."
        )
    )
    media_type: str = Field(description="MIME type (e.g. 'image/png').")
    sha256: str | None = Field(default=None, description="SHA-256 hash of asset content.")
    size_bytes: int | None = Field(default=None, ge=0, description="File size in bytes.")
    width: int | None = Field(default=None, gt=0, description="Image/video width in pixels.")
    height: int | None = Field(default=None, gt=0, description="Image/video height in pixels.")
    duration: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, description="Audio/video duration in seconds."
    )
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

    # NOTE: sha256 is intentionally NOT format-validated here. Asset must tolerate
    # a malformed/legacy sha256 on construction and load (see #100 and
    # test_asset_tolerates_malformed_sha256_on_load) so that parse_manifest() /
    # extract_manifest() never crash on old or foreign-authored manifests.
    # Rejection happens at the verification boundary instead — see
    # is_valid_sha256() + Manifest.output_asset_ids_missing_sha256() /
    # ManifestVerification, which already make a malformed sha256 fail
    # manifest.verify() (confirmed: it no longer returns True as #78 originally
    # reported — that was fixed by #100's verification hardening).

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str) -> str:
        if not _MEDIA_TYPE_RE.match(value):
            raise ValueError(f"media_type must look like 'type/subtype', got {value!r}")
        return value

    def set_hash(self, data: bytes) -> None:
        """Compute and set sha256 + size_bytes from raw asset bytes."""
        self.sha256 = compute_sha256(data)
        self.size_bytes = len(data)

    def __repr__(self) -> str:
        return f"Asset(id={self.asset_id[:8]}..., url={self.url!r}, type={self.media_type})"
