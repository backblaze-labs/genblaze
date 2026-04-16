"""FFmpegTransform — asset transformation provider for resize, crop, overlay, and more.

Uses ffmpeg subprocess for media transformations. Follows the same pattern
as FFmpegCompositor but operates on a single input asset.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepType
from genblaze_core.models.step import Step
from genblaze_core.providers._ffmpeg_utils import (
    FFMPEG_TIMEOUT,
    get_output_path,
    resolve_ffmpeg,
    resolve_input_path,
    run_ffmpeg,
)
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

logger = logging.getLogger("genblaze.transform")

# Supported operations and their step types
_OPERATION_STEP_TYPES: dict[str, StepType] = {
    "resize": StepType.TRANSCODE,
    "crop": StepType.EDIT,
    "overlay_text": StepType.EDIT,
    "audio_normalize": StepType.TRANSCODE,
    "convert_format": StepType.TRANSCODE,
}

# Allowed output formats for convert_format operation
_ALLOWED_FORMATS = {"mp4", "webm", "mkv", "mov", "mp3", "wav", "flac", "ogg", "aac"}

# Safe color values for ffmpeg drawtext (named colors, hex #RRGGBB/#RRGGBBAA)
_SAFE_COLOR_RE = re.compile(r"^[a-zA-Z0-9#]{1,16}$")

# Media type mapping for convert_format
_FORMAT_MEDIA_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "aac": "audio/aac",
}


def _escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext filter.

    Colons, backslashes, and single quotes need escaping in filter syntax.
    """
    # Escape backslashes first, then colons and quotes
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    return text


class FFmpegTransform(SyncProvider):
    """Asset transformation provider using ffmpeg.

    Operates on a single input asset from step.inputs. The transformation
    is selected via step.params["operation"].

    Supported operations:
        resize: Scale to width x height (-vf scale=w:h)
        crop: Crop region (-vf crop=w:h:x:y)
        overlay_text: Draw text overlay (-vf drawtext=...)
        audio_normalize: Normalize audio loudness (-af loudnorm)
        convert_format: Convert container/codec format

    Args:
        output_dir: Directory for output files (default system temp).
        ffmpeg_path: Path to ffmpeg binary (default "ffmpeg" — uses PATH).
        timeout: Subprocess timeout in seconds (default 120).
    """

    name = "ffmpeg-transform"

    def __init__(
        self,
        output_dir: str | Path | None = None,
        ffmpeg_path: str = "ffmpeg",
        timeout: float = FFMPEG_TIMEOUT,
    ):
        super().__init__()
        self._output_dir = Path(output_dir) if output_dir else None
        self._ffmpeg_path = ffmpeg_path
        self._timeout = timeout

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO, Modality.AUDIO],
            supported_inputs=["video", "audio"],
            accepts_chain_input=True,
            output_formats=["video/mp4", "video/webm", "audio/mpeg", "audio/wav"],
        )

    def _build_resize_cmd(
        self,
        ffmpeg_bin: str,
        input_path: str,
        out_path: str,
        params: dict,
    ) -> list[str]:
        """Build ffmpeg command for resize operation."""
        try:
            width = int(params["width"])
            height = int(params["height"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(
                f"resize requires integer 'width' and 'height' params: {exc}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            ) from exc
        return [
            ffmpeg_bin,
            "-i",
            input_path,
            "-vf",
            f"scale={width}:{height}",
            "-y",
            out_path,
        ]

    def _build_crop_cmd(
        self,
        ffmpeg_bin: str,
        input_path: str,
        out_path: str,
        params: dict,
    ) -> list[str]:
        """Build ffmpeg command for crop operation."""
        try:
            width = int(params["width"])
            height = int(params["height"])
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(
                f"crop requires integer 'width', 'height' (and optional 'x', 'y') params: {exc}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            ) from exc
        return [
            ffmpeg_bin,
            "-i",
            input_path,
            "-vf",
            f"crop={width}:{height}:{x}:{y}",
            "-y",
            out_path,
        ]

    def _build_overlay_text_cmd(
        self,
        ffmpeg_bin: str,
        input_path: str,
        out_path: str,
        params: dict,
    ) -> list[str]:
        """Build ffmpeg command for text overlay."""
        text = params.get("text")
        if not text:
            raise ProviderError(
                "overlay_text requires 'text' param",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        # Validate numeric params to prevent ffmpeg filter injection
        try:
            fontsize = int(params.get("fontsize", 24))
            x = int(params.get("x", 10))
            y = int(params.get("y", 10))
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                f"overlay_text fontsize/x/y must be integers: {exc}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            ) from exc

        fontcolor = str(params.get("fontcolor", "white"))
        if not _SAFE_COLOR_RE.match(fontcolor):
            raise ProviderError(
                f"Invalid fontcolor '{fontcolor}'. Use alphanumeric or hex color values.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )

        escaped = _escape_drawtext(text)
        drawtext = (
            f"drawtext=text='{escaped}':fontsize={fontsize}:x={x}:y={y}:fontcolor={fontcolor}"
        )
        return [
            ffmpeg_bin,
            "-i",
            input_path,
            "-vf",
            drawtext,
            "-y",
            out_path,
        ]

    def _build_audio_normalize_cmd(
        self,
        ffmpeg_bin: str,
        input_path: str,
        out_path: str,
        params: dict,
    ) -> list[str]:
        """Build ffmpeg command for audio normalization."""
        # For video inputs, copy video stream and only process audio
        return [
            ffmpeg_bin,
            "-i",
            input_path,
            "-c:v",
            "copy",
            "-af",
            "loudnorm",
            "-y",
            out_path,
        ]

    def _build_convert_format_cmd(
        self,
        ffmpeg_bin: str,
        input_path: str,
        out_path: str,
        params: dict,
    ) -> list[str]:
        """Build ffmpeg command for format conversion."""
        # Format validation already done in generate(); ffmpeg infers codec from extension
        return [
            ffmpeg_bin,
            "-i",
            input_path,
            "-y",
            out_path,
        ]

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Apply transformation to the input asset."""
        ffmpeg_bin = resolve_ffmpeg(self._ffmpeg_path)

        if not step.inputs:
            raise ProviderError(
                "FFmpegTransform requires at least one input asset in step.inputs",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        input_asset = step.inputs[0]
        roots = [self._output_dir] if self._output_dir else None
        input_path = resolve_input_path(input_asset.url, extra_roots=roots)

        operation = step.params.get("operation")
        if not operation:
            raise ProviderError(
                "FFmpegTransform requires 'operation' param. "
                f"Supported: {', '.join(sorted(_OPERATION_STEP_TYPES))}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        if operation not in _OPERATION_STEP_TYPES:
            raise ProviderError(
                f"Unknown operation '{operation}'. "
                f"Supported: {', '.join(sorted(_OPERATION_STEP_TYPES))}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )

        if operation == "convert_format":
            fmt = step.params.get("format", "mp4")
            if fmt not in _ALLOWED_FORMATS:
                raise ProviderError(
                    f"Unsupported format '{fmt}'. Allowed: {', '.join(sorted(_ALLOWED_FORMATS))}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            ext = fmt
        else:
            # Preserve input format
            ext = _ext_from_media_type(input_asset.media_type)

        out_path = get_output_path(step.step_id, ext, self._output_dir)

        builders = {
            "resize": self._build_resize_cmd,
            "crop": self._build_crop_cmd,
            "overlay_text": self._build_overlay_text_cmd,
            "audio_normalize": self._build_audio_normalize_cmd,
            "convert_format": self._build_convert_format_cmd,
        }
        cmd = builders[operation](ffmpeg_bin, input_path, str(out_path), step.params)

        run_ffmpeg(cmd, timeout=self._timeout)

        file_url = f"file://{quote(str(out_path.resolve()))}"
        if operation == "convert_format":
            media_type = _FORMAT_MEDIA_TYPES.get(ext, input_asset.media_type)
        else:
            media_type = input_asset.media_type

        asset = Asset(url=file_url, media_type=media_type)

        if operation == "resize":
            asset.width = step.params.get("width")
            asset.height = step.params.get("height")
            asset.duration = input_asset.duration
            if input_asset.video:
                asset.video = VideoMetadata(
                    codec=input_asset.video.codec,
                    frame_rate=input_asset.video.frame_rate,
                    has_audio=input_asset.video.has_audio,
                )
        elif operation == "crop":
            asset.width = step.params.get("width")
            asset.height = step.params.get("height")
            asset.duration = input_asset.duration
            if input_asset.video:
                asset.video = VideoMetadata(
                    codec=input_asset.video.codec,
                    frame_rate=input_asset.video.frame_rate,
                    has_audio=input_asset.video.has_audio,
                )
        elif operation == "overlay_text":
            asset.width = input_asset.width
            asset.height = input_asset.height
            asset.duration = input_asset.duration
            if input_asset.video:
                asset.video = input_asset.video.model_copy()
        elif operation == "audio_normalize":
            asset.duration = input_asset.duration
            if input_asset.audio:
                asset.audio = AudioMetadata(
                    codec=input_asset.audio.codec,
                    channels=input_asset.audio.channels,
                    sample_rate=input_asset.audio.sample_rate,
                )
            if input_asset.video:
                asset.video = input_asset.video.model_copy()
                asset.width = input_asset.width
                asset.height = input_asset.height
        elif operation == "convert_format":
            asset.width = input_asset.width
            asset.height = input_asset.height
            asset.duration = input_asset.duration

        try:
            asset.size_bytes = out_path.stat().st_size
        except OSError:
            pass

        step.assets.append(asset)
        step.step_type = _OPERATION_STEP_TYPES[operation]

        return step


def _ext_from_media_type(media_type: str) -> str:
    """Extract file extension from media type."""
    mapping = {
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/x-matroska": "mkv",
        "video/quicktime": "mov",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/flac": "flac",
        "audio/ogg": "ogg",
        "audio/aac": "aac",
    }
    return mapping.get(media_type, "mp4")
