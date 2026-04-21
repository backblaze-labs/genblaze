"""Tests for FFmpegCompositor provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, VideoMetadata
from genblaze_core.models.enums import Modality, StepStatus, StepType
from genblaze_core.models.step import Step
from genblaze_core.providers.compositor import FFmpegCompositor

_UTILS = "genblaze_core.providers._ffmpeg_utils"


def _make_video_asset() -> Asset:
    """Create a video asset with metadata for testing."""
    asset = Asset(url="file:///tmp/test_video.mp4", media_type="video/mp4")
    asset.video = VideoMetadata(codec="h264", frame_rate=24.0, has_audio=False, resolution="1080p")
    asset.width = 1920
    asset.height = 1080
    asset.duration = 10.0
    return asset


def _make_audio_asset() -> Asset:
    """Create an audio asset with metadata for testing."""
    asset = Asset(url="file:///tmp/test_audio.mp3", media_type="audio/mpeg")
    asset.audio = AudioMetadata(codec="mp3", channels=2, sample_rate=44100)
    return asset


def _make_step(video: Asset | None = None, audio: Asset | None = None) -> Step:
    """Build a compositor step with video and audio inputs."""
    inputs = []
    if video:
        inputs.append(video)
    if audio:
        inputs.append(audio)
    return Step(
        provider="ffmpeg-compositor",
        model="mux",
        prompt=None,
        step_type=StepType.MIX,
        modality=Modality.VIDEO,
        inputs=inputs,
    )


# --- Core generate tests ---


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_generate_muxes_video_and_audio(mock_run, mock_which):
    """Verify ffmpeg is called with correct args to mux video + audio."""
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")

    compositor = FFmpegCompositor()
    step = _make_step(_make_video_asset(), _make_audio_asset())

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=5_000_000)
        result = compositor.generate(step)

    # Verify ffmpeg was called with the expected arguments
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "-i" in cmd
    assert "-c" in cmd
    assert "copy" in cmd
    assert "-shortest" in cmd
    assert cmd[-1].endswith(".mp4")

    # Verify two -i flags for video and audio inputs
    i_indices = [i for i, arg in enumerate(cmd) if arg == "-i"]
    assert len(i_indices) == 2

    # Output asset created
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"


def test_generate_no_video_input_raises():
    """Error when no video asset in step.inputs."""
    compositor = FFmpegCompositor()
    step = _make_step(audio=_make_audio_asset())

    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="No video asset"):
            compositor.generate(step)


def test_generate_no_audio_input_raises():
    """Error when no audio asset in step.inputs."""
    compositor = FFmpegCompositor()
    step = _make_step(video=_make_video_asset())

    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="No audio asset"):
            compositor.generate(step)


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_output_has_tracks_and_metadata(mock_run, mock_which):
    """Verify Track list and VideoMetadata on the output asset."""
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")

    compositor = FFmpegCompositor()
    step = _make_step(_make_video_asset(), _make_audio_asset())

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=5_000_000)
        result = compositor.generate(step)

    asset = result.assets[0]

    # Video metadata copied from input, has_audio=True
    assert asset.video is not None
    assert asset.video.has_audio is True
    assert asset.video.codec == "h264"
    assert asset.video.frame_rate == 24.0
    assert asset.video.resolution == "1080p"

    # Audio metadata copied from input
    assert asset.audio is not None
    assert asset.audio.codec == "mp3"
    assert asset.audio.channels == 2
    assert asset.audio.sample_rate == 44100

    # Track list describes container contents
    assert asset.tracks is not None
    assert len(asset.tracks) == 2
    assert asset.tracks[0].kind == "video"
    assert asset.tracks[0].codec == "h264"
    assert asset.tracks[1].kind == "audio"
    assert asset.tracks[1].codec == "mp3"

    # Dimensions and duration copied from video input
    assert asset.width == 1920
    assert asset.height == 1080
    assert asset.duration == 10.0

    # Step type set to MIX
    assert result.step_type == StepType.MIX


def test_ffmpeg_not_found_raises():
    """Clear error when ffmpeg is not installed."""
    compositor = FFmpegCompositor()
    step = _make_step(_make_video_asset(), _make_audio_asset())

    with patch(f"{_UTILS}.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="ffmpeg not found"):
            compositor.generate(step)


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_ffmpeg_nonzero_exit_raises(mock_run, mock_which):
    """Non-zero ffmpeg exit code raises ProviderError with stderr."""
    mock_run.return_value = MagicMock(returncode=1, stderr=b"Error: invalid input file")

    compositor = FFmpegCompositor()
    step = _make_step(_make_video_asset(), _make_audio_asset())

    with pytest.raises(ProviderError, match="ffmpeg exited with code 1"):
        compositor.generate(step)


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_ffmpeg_timeout_raises(mock_run, mock_which):
    """Subprocess timeout raises ProviderError with TIMEOUT code."""
    import subprocess

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)

    compositor = FFmpegCompositor(timeout=120)
    step = _make_step(_make_video_asset(), _make_audio_asset())

    with pytest.raises(ProviderError, match="ffmpeg timed out"):
        compositor.generate(step)


def test_compositor_in_pipeline():
    """Generate video + audio, then mux with FFmpegCompositor via invoke()."""
    from genblaze_core.pipeline import Pipeline
    from genblaze_core.testing import MockAudioProvider, MockVideoProvider

    video_prov = MockVideoProvider()
    audio_prov = MockAudioProvider()

    # Step 1+2: generate video and audio via pipeline
    result = (
        Pipeline("av-gen")
        .step(video_prov, model="vid", prompt="sunset timelapse")
        .step(audio_prov, model="aud", prompt="ocean waves")
        .run()
    )
    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert result.run.steps[1].status == StepStatus.SUCCEEDED

    # Step 3: mux using compositor with both outputs as inputs
    video_asset = result.run.steps[0].assets[0]
    audio_asset = result.run.steps[1].assets[0]

    compositor = FFmpegCompositor()
    mux_step = Step(
        provider="ffmpeg-compositor",
        model="mux",
        step_type=StepType.MIX,
        modality=Modality.VIDEO,
        inputs=[video_asset, audio_asset],
    )

    with (
        patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(f"{_UTILS}.subprocess.run") as mock_run,
        patch("pathlib.Path.stat") as mock_stat,
        # SSRF guard resolves DNS for https chain inputs; the mock providers
        # use example.test/mock.test hosts that don't resolve, so stub the
        # resolver with a public IP to exercise the happy path.
        patch(
            "genblaze_core._utils.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        mock_stat.return_value = MagicMock(st_size=8_000_000)
        mux_result = compositor.invoke(mux_step)

    assert mux_result.status == StepStatus.SUCCEEDED
    assert len(mux_result.assets) == 1
    assert mux_result.assets[0].media_type == "video/mp4"
    assert mux_result.assets[0].video.has_audio is True


def test_get_capabilities():
    """Compositor declares correct capabilities."""
    compositor = FFmpegCompositor()
    caps = compositor.get_capabilities()
    assert caps.supported_modalities == [Modality.VIDEO]
    assert caps.supported_inputs == ["video", "audio"]
    assert caps.output_formats == ["video/mp4"]


def test_importable_from_top_level():
    """FFmpegCompositor is accessible via genblaze_core top-level import."""
    from genblaze_core import FFmpegCompositor as FC

    assert FC is FFmpegCompositor


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
@patch(
    "genblaze_core._utils.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
)
def test_https_input_urls_passed_directly(mock_dns, mock_run, mock_which):
    """HTTPS URLs are passed directly to ffmpeg (it supports them natively)."""
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")

    video = Asset(url="https://cdn.example.com/video.mp4", media_type="video/mp4")
    video.video = VideoMetadata(codec="h264")
    audio = Asset(url="https://cdn.example.com/audio.mp3", media_type="audio/mpeg")
    audio.audio = AudioMetadata(codec="mp3")

    compositor = FFmpegCompositor()
    step = _make_step(video, audio)

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=1_000_000)
        compositor.generate(step)

    cmd = mock_run.call_args[0][0]
    # HTTPS URLs should appear as-is in the ffmpeg command
    assert "https://cdn.example.com/video.mp4" in cmd
    assert "https://cdn.example.com/audio.mp3" in cmd


def test_unsupported_url_scheme_raises():
    """Unsupported URL scheme (e.g. ftp://) raises ProviderError."""
    video = Asset(url="ftp://example.com/video.mp4", media_type="video/mp4")
    audio = _make_audio_asset()

    compositor = FFmpegCompositor()
    step = _make_step(video, audio)

    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="Unsupported URL scheme.*ffmpeg input"):
            compositor.generate(step)
