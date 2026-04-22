<!-- last_verified: 2026-04-22 -->
# genblaze-stability-audio

**[Stability AI Stable Audio](https://stability.ai/stable-audio) music + ambient sound provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — AI music generation pipelines with SHA-256 provenance manifests on every track.**

`genblaze-stability-audio` wraps Stability AI's Stable Audio 2.5 API as a genblaze provider — up to 3 minutes of 44.1 kHz stereo music, soundtrack, or ambient audio generated from a text prompt. Compose into multi-step AI pipelines (e.g. TTS narration + Stable Audio soundtrack → AV composite), persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-stability-audio

- **Stable Audio 2.5** — up to 3 minutes, 44.1 kHz stereo, text-to-music / text-to-ambient.
- **Provenance by default** — SHA-256-verified manifest with prompt, model, duration, and timestamps.
- **Composable** — chain with TTS providers and `FFmpegCompositor` for narration + soundtrack AV composites.
- **Production-ready** — retries, timeouts, step caching, moderation hooks.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Models

| Model | Notes |
|---|---|
| `stable-audio-2.5` | Up to 3 minutes of music/ambient audio at 44.1 kHz stereo |

## Install

```bash
pip install genblaze-stability-audio
```

Registers the `stability-audio` provider via entry points; [`genblaze-core`](https://pypi.org/project/genblaze-core/) discovers it automatically.

## Quickstart — text-to-music

```bash
pip install genblaze-core genblaze-stability-audio
export STABILITY_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_stability_audio import StabilityAudioProvider

run, manifest = (
    Pipeline("soundtrack")
    .step(StabilityAudioProvider(output_dir="output/music"),
          model="stable-audio-2.5",
          prompt="Upbeat lo-fi hip hop beat with warm piano chords and vinyl crackle",
          modality=Modality.AUDIO, duration=30, output_format="mp3")
    .run(timeout=120)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

## Persist to Backblaze B2

```python
from genblaze_core import KeyStrategy, ObjectStorageSink
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)
# pass sink=storage to .run(…)
```

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for AI-generated audio — cost-efficient, S3-compatible.

## Credentials

| Env var | Where to get it |
|---|---|
| `STABILITY_API_KEY` | https://platform.stability.ai/account/keys |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Example**: [`stability_audio_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/stability_audio_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends
- Other audio providers: [`genblaze-elevenlabs`](https://pypi.org/project/genblaze-elevenlabs/) (TTS + SFX) · [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) (TTS) · [`genblaze-lmnt`](https://pypi.org/project/genblaze-lmnt/) (fast TTS) · [`genblaze-gmicloud`](https://pypi.org/project/genblaze-gmicloud/)

## License

MIT
