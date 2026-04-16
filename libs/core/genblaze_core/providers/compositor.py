"""FFmpegCompositor — local compositor that muxes video + audio into MP4.

Uses ffmpeg subprocess to combine video and audio assets without re-encoding.
Requires ffmpeg installed on the system (checked at generate-time).
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, Track, VideoMetadata
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

logger = logging.getLogger("genblaze.compositor")


def _find_asset_by_type(assets: list[Asset], prefix: str) -> Asset | None:
    """Find the first asset whose media_type starts with prefix (e.g. 'video/')."""
    for asset in assets:
        if asset.media_type.startswith(prefix):
            return asset
    return None


class FFmpegCompositor(SyncProvider):
    """Local compositor that muxes video + audio into MP4 using ffmpeg.

    Expects step.inputs to contain at least one video and one audio asset.
    Uses ffmpeg subprocess — requires ffmpeg installed on the system.

    Args:
        output_dir: Directory for output files (default system temp).
        ffmpeg_path: Path to ffmpeg binary (default "ffmpeg" — uses PATH).
        timeout: Subprocess timeout in seconds (default 120).
    """

    name = "ffmpeg-compositor"

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
        """FFmpeg compositor: muxes video + audio into a single MP4 container."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["video", "audio"],
            accepts_chain_input=True,
            output_formats=["video/mp4"],
        )

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        """Mux video + audio from step.inputs into a single MP4."""
        ffmpeg_bin = resolve_ffmpeg(self._ffmpeg_path)

        video_asset = _find_asset_by_type(step.inputs, "video/")
        audio_asset = _find_asset_by_type(step.inputs, "audio/")

        if video_asset is None:
            raise ProviderError(
                "No video asset found in step.inputs. "
                "Compositor requires at least one video/ input.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        if audio_asset is None:
            raise ProviderError(
                "No audio asset found in step.inputs. "
                "Compositor requires at least one audio/ input.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )

        roots = [self._output_dir] if self._output_dir else None
        video_path = resolve_input_path(video_asset.url, extra_roots=roots)
        audio_path = resolve_input_path(audio_asset.url, extra_roots=roots)
        out_path = get_output_path(step.step_id, "mp4", self._output_dir)

        # Build ffmpeg command: mux without re-encoding, trim to shorter input
        cmd = [
            ffmpeg_bin,
            "-i",
            video_path,
            "-i",
            audio_path,
            "-c",
            "copy",
            "-shortest",
            "-y",
            str(out_path),
        ]

        run_ffmpeg(cmd, timeout=self._timeout)

        file_url = f"file://{quote(str(out_path.resolve()))}"
        asset = Asset(url=file_url, media_type="video/mp4")

        video_meta = VideoMetadata(has_audio=True)
        if video_asset.video:
            video_meta.codec = video_asset.video.codec
            video_meta.frame_rate = video_asset.video.frame_rate
            video_meta.resolution = video_asset.video.resolution
        asset.video = video_meta

        audio_meta = AudioMetadata()
        if audio_asset.audio:
            audio_meta.codec = audio_asset.audio.codec
            audio_meta.channels = audio_asset.audio.channels
            audio_meta.sample_rate = audio_asset.audio.sample_rate
        asset.audio = audio_meta

        asset.tracks = [
            Track(kind="video", codec=video_meta.codec, label="source-video"),
            Track(kind="audio", codec=audio_meta.codec, label="source-audio"),
        ]

        asset.width = video_asset.width
        asset.height = video_asset.height
        asset.duration = video_asset.duration

        try:
            asset.size_bytes = out_path.stat().st_size
        except OSError:
            pass

        step.assets.append(asset)
        step.step_type = StepType.MIX

        return step
