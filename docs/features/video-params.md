<!-- last_verified: 2026-07-07 -->
# Video & Audio Parameter Conventions

Standard `step.params` keys that video/audio providers should map from.

## Video Parameters

| Key | Type | Values | Description |
|-----|------|--------|-------------|
| `camera_motion`* | str | `"pan_left"`, `"pan_right"`, `"zoom_in"`, `"zoom_out"`, `"orbit"`, `"static"` | Camera movement direction |
| `motion_intensity`* | float | 0.0–1.0 | How much motion in the generated video |
| `fps`* | int | 24, 30, 60 | Target frames per second |
| `resolution` | str | `"720p"`, `"1080p"`, `"4k"` | Output resolution |
| `aspect_ratio` | str | `"16:9"`, `"9:16"`, `"1:1"` | Output aspect ratio |
| `duration` | float | seconds | Video duration |
| `edit_type` | str | `"extend"`, `"inpaint"`, `"outpaint"`, `"style_transfer"` | For EDIT step type |

*\* Documented conventions — not yet mapped by any provider. Providers will adopt these as their APIs add support.*

## Provider Mapping

| Standard Key | Runway | Luma | Google Veo | GMICloud video |
|-------------|--------|------|------------|----------------|
| `aspect_ratio` | `ratio` | `aspect_ratio` (native) | `aspect_ratio` (native) | `aspect_ratio` (native) |
| `duration` | `duration` (native, int) | `duration` | `duration_seconds` | `duration` (native, whole-second int) |
| `resolution` | — | `resolution` (native) | `resolution` (native) | `resolution` (native) |

## Audio Asset Metadata

Providers populate `asset.metadata` with output properties:

### Video Output

- `fps`: int — actual frames per second
- `codec`: str — e.g. "h264", "vp9"
- `bitrate`: str — e.g. "5000k"
- `has_audio`: bool — whether the video includes an audio track

### Audio Output

- `audio_type`: str — `"speech"`, `"music"`, or `"sfx"`
- `sample_rate`: int — e.g. 44100, 22050
- `channels`: int — 1 (mono) or 2 (stereo)

### Music-Specific

- `bpm`: int — beats per minute (user-supplied via params, echoed to metadata)
- `key`: str — musical key (e.g. "C major")
- `genre`: str — genre label

### Speech-Specific

- `word_timings`: list[dict] — word-level timing data with `text`, `start`, `end` keys

## Asset Duration

Audio providers set `asset.duration` (float, seconds) on output assets:

- **LMNT**: computed from word durations
- **Stability Audio**: from `step.params["duration"]`
- **ElevenLabs SFX**: from `step.params["duration_seconds"]`
