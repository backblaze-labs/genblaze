---
type: Library
title: Asset transforms
description: Local transform steps that post-process prior step outputs.
status: stable
stale_after: 2026-12-17T00:00:00Z
owner: "@jeronimodeleon"
---
# Asset Transforms

`FFmpegTransform` is a `SyncProvider` that performs media transformations via ffmpeg. It operates on a single input asset and supports resize, crop, text overlay, audio normalization, and format conversion.

## Usage

```python
from genblaze_core import Pipeline, Modality, FFmpegTransform, StepType

transform = FFmpegTransform(output_dir="output/")

# Resize after generation
result = (
    Pipeline("resize", chain=True)
    .step(video_provider, model="ray-2", prompt="sunset", modality=Modality.VIDEO)
    .step(transform, model="transform", modality=Modality.VIDEO,
          step_type=StepType.TRANSCODE, operation="resize", width=1280, height=720)
    .run()
)
```

## Supported Operations

| Operation | step_type | Required params | ffmpeg filter |
|---|---|---|---|
| `resize` | TRANSCODE | `width`, `height` | `-vf scale=w:h` |
| `crop` | EDIT | `width`, `height`, `x`(0), `y`(0) | `-vf crop=w:h:x:y` |
| `overlay_text` | EDIT | `text`, `fontsize`(24), `x`(10), `y`(10), `fontcolor`("white") | `-vf drawtext=...` |
| `audio_normalize` | TRANSCODE | (none) | `-af loudnorm` |
| `convert_format` | TRANSCODE | `format` (mp4, webm, mkv, mov, mp3, wav, flac, ogg, aac) | output extension |

## Architecture

Shared ffmpeg utilities (`_ffmpeg_utils.py`) are used by both `FFmpegCompositor` and `FFmpegTransform`:
- `resolve_ffmpeg()` — validates ffmpeg is installed
- `resolve_input_path()` — resolves file:// and https:// URLs
- `run_ffmpeg()` — subprocess execution with timeout and error mapping

## Canonical files

- `libs/core/genblaze_core/providers/transform.py`
- `libs/core/genblaze_core/providers/_ffmpeg_utils.py`
