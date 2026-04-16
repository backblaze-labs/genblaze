"""Tests for FFmpegTransform provider."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, VideoMetadata
from genblaze_core.models.enums import Modality, StepStatus, StepType
from genblaze_core.models.step import Step
from genblaze_core.providers.transform import FFmpegTransform, _escape_drawtext

_UTILS = "genblaze_core.providers._ffmpeg_utils"


def _make_video_asset() -> Asset:
    asset = Asset(url="file:///tmp/input.mp4", media_type="video/mp4")
    asset.video = VideoMetadata(codec="h264", frame_rate=30.0, has_audio=True)
    asset.width = 1920
    asset.height = 1080
    asset.duration = 15.0
    return asset


def _make_audio_asset() -> Asset:
    asset = Asset(url="file:///tmp/input.mp3", media_type="audio/mpeg")
    asset.audio = AudioMetadata(codec="mp3", channels=2, sample_rate=44100)
    asset.duration = 60.0
    return asset


def _make_step(operation: str, input_asset: Asset | None = None, **params) -> Step:
    inputs = [input_asset] if input_asset else []
    return Step(
        provider="ffmpeg-transform",
        model="transform",
        prompt=None,
        modality=Modality.VIDEO,
        inputs=inputs,
        params={"operation": operation, **params},
    )


# --- Operation tests ---


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_resize_builds_correct_filter(mock_run, mock_which):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    transform = FFmpegTransform()
    step = _make_step("resize", _make_video_asset(), width=1280, height=720)

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=1_000_000)
        result = transform.generate(step)

    cmd = mock_run.call_args[0][0]
    assert "-vf" in cmd
    vf_idx = cmd.index("-vf")
    assert cmd[vf_idx + 1] == "scale=1280:720"
    assert result.assets[0].width == 1280
    assert result.assets[0].height == 720
    assert result.step_type == StepType.TRANSCODE


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_crop_builds_correct_filter(mock_run, mock_which):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    transform = FFmpegTransform()
    step = _make_step("crop", _make_video_asset(), width=640, height=480, x=100, y=50)

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=500_000)
        result = transform.generate(step)

    cmd = mock_run.call_args[0][0]
    vf_idx = cmd.index("-vf")
    assert cmd[vf_idx + 1] == "crop=640:480:100:50"
    assert result.assets[0].width == 640
    assert result.assets[0].height == 480
    assert result.step_type == StepType.EDIT


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_overlay_text_builds_drawtext(mock_run, mock_which):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    transform = FFmpegTransform()
    step = _make_step(
        "overlay_text",
        _make_video_asset(),
        text="Hello World",
        fontsize=32,
        x=50,
        y=50,
        fontcolor="yellow",
    )

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=1_000_000)
        result = transform.generate(step)

    cmd = mock_run.call_args[0][0]
    vf_idx = cmd.index("-vf")
    drawtext_arg = cmd[vf_idx + 1]
    assert "drawtext=" in drawtext_arg
    assert "Hello World" in drawtext_arg
    assert "fontsize=32" in drawtext_arg
    assert "fontcolor=yellow" in drawtext_arg
    # Dimensions unchanged
    assert result.assets[0].width == 1920
    assert result.assets[0].height == 1080
    assert result.step_type == StepType.EDIT


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_audio_normalize_applies_loudnorm(mock_run, mock_which):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    transform = FFmpegTransform()
    step = _make_step("audio_normalize", _make_audio_asset())
    step.modality = Modality.AUDIO

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=500_000)
        result = transform.generate(step)

    cmd = mock_run.call_args[0][0]
    assert "-af" in cmd
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "loudnorm"
    assert result.step_type == StepType.TRANSCODE
    assert result.assets[0].audio is not None
    assert result.assets[0].audio.codec == "mp3"


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_convert_format_changes_extension(mock_run, mock_which):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    transform = FFmpegTransform()
    step = _make_step("convert_format", _make_video_asset(), format="webm")

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=800_000)
        result = transform.generate(step)

    cmd = mock_run.call_args[0][0]
    assert cmd[-1].endswith(".webm")
    assert result.assets[0].media_type == "video/webm"
    assert result.step_type == StepType.TRANSCODE


# --- Error cases ---


def test_missing_operation_raises():
    transform = FFmpegTransform()
    step = Step(
        provider="ffmpeg-transform",
        model="t",
        modality=Modality.VIDEO,
        inputs=[_make_video_asset()],
        params={},
    )
    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="requires 'operation' param"):
            transform.generate(step)


def test_unknown_operation_raises():
    transform = FFmpegTransform()
    step = _make_step("blur", _make_video_asset())
    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="Unknown operation 'blur'"):
            transform.generate(step)


def test_no_input_asset_raises():
    transform = FFmpegTransform()
    step = Step(
        provider="ffmpeg-transform",
        model="t",
        modality=Modality.VIDEO,
        inputs=[],
        params={"operation": "resize", "width": 640, "height": 480},
    )
    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="requires at least one input"):
            transform.generate(step)


def test_ffmpeg_not_found_raises():
    transform = FFmpegTransform()
    step = _make_step("resize", _make_video_asset(), width=640, height=480)
    with patch(f"{_UTILS}.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="ffmpeg not found"):
            transform.generate(step)


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_ffmpeg_timeout_raises(mock_run, mock_which):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)
    transform = FFmpegTransform()
    step = _make_step("resize", _make_video_asset(), width=640, height=480)
    with pytest.raises(ProviderError, match="ffmpeg timed out"):
        transform.generate(step)


def test_resize_missing_dimensions_raises():
    transform = FFmpegTransform()
    step = _make_step("resize", _make_video_asset())  # no width/height
    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="resize requires"):
            transform.generate(step)


def test_convert_format_unsupported_raises():
    transform = FFmpegTransform()
    step = _make_step("convert_format", _make_video_asset(), format="exe")
    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="Unsupported format 'exe'"):
            transform.generate(step)


def test_overlay_text_missing_text_raises():
    transform = FFmpegTransform()
    step = _make_step("overlay_text", _make_video_asset())  # no text param
    with patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ProviderError, match="requires 'text' param"):
            transform.generate(step)


# --- Drawtext escaping ---


def test_escape_drawtext_colons():
    assert _escape_drawtext("time: 12:30") == "time\\: 12\\:30"


def test_escape_drawtext_quotes():
    assert _escape_drawtext("it's") == "it\\'s"


# --- Capabilities and integration ---


def test_get_capabilities():
    transform = FFmpegTransform()
    caps = transform.get_capabilities()
    assert Modality.VIDEO in caps.supported_modalities
    assert Modality.AUDIO in caps.supported_modalities
    assert caps.accepts_chain_input is True


@patch(f"{_UTILS}.shutil.which", return_value="/usr/bin/ffmpeg")
@patch(f"{_UTILS}.subprocess.run")
def test_transform_in_pipeline_chain(mock_run, mock_which):
    """Transform works as a chained step after generation."""
    from genblaze_core.pipeline.pipeline import Pipeline
    from genblaze_core.testing import MockVideoProvider

    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    video_prov = MockVideoProvider()
    transform = FFmpegTransform()

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=500_000)
        result = (
            Pipeline("chain-test", chain=True)
            .step(video_prov, model="vid", prompt="sunset", modality=Modality.VIDEO)
            .step(
                transform,
                model="transform",
                modality=Modality.VIDEO,
                step_type=StepType.TRANSCODE,
                operation="resize",
                width=640,
                height=480,
            )
            .run()
        )

    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert result.run.steps[1].status == StepStatus.SUCCEEDED
    assert result.run.steps[1].assets[0].width == 640
