<!-- last_verified: 2026-04-22 -->
# genblaze-openai

**OpenAI provider adapters for [genblaze](https://github.com/backblaze-labs/genblaze) — [Sora](https://openai.com/sora) text-to-video, DALL·E / gpt-image text-to-image, and TTS text-to-speech — with SHA-256 provenance manifests on every output.**

`genblaze-openai` wraps OpenAI's generative media APIs (Sora video, DALL·E 3 and gpt-image-1 images, `tts-1` / `tts-1-hd` / `gpt-4o-mini-tts` audio) as genblaze providers. Compose them into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-openai

- **Three OpenAI modalities, one SDK** — video (Sora), image (DALL·E, gpt-image), audio (TTS) — same `Pipeline` API.
- **Built-in provenance** — Every Sora render / DALL·E image / TTS clip lands with a SHA-256-verified manifest.
- **Swap models without rewrites** — Same pipeline works with Runway, Luma, Flux, Veo, ElevenLabs, etc.
- **Production-ready** — Retries, timeouts, moderation hooks, step caching, streaming events.
- **Durable storage** — Plug `genblaze-s3` in for B2 / AWS S3 / R2 / MinIO persistence.

## Providers + models

| Provider class | Modality | Models |
|---|---|---|
| `SoraProvider` | video | `sora-2`, `sora-2-pro` |
| `DalleProvider` | image | `gpt-image-1`, `dall-e-3`, `dall-e-2` (+ edits) |
| `OpenAITTSProvider` | audio | `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts` |

Each is registered via entry points (`openai-sora`, `openai-dalle`, `openai-tts`).

## Install

```bash
pip install genblaze-openai
```

## Quickstart — Sora text-to-video

```bash
export OPENAI_API_KEY="sk-..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_openai import SoraProvider

run, manifest = (
    Pipeline("sora-demo")
    .step(SoraProvider(), model="sora-2",
          prompt="A cinematic drone shot gliding over a misty mountain valley at sunrise",
          modality=Modality.VIDEO, seconds=4, size="1280x720")
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

## Quickstart — DALL·E text-to-image

```python
from genblaze_openai import DalleProvider

run, manifest = (
    Pipeline("dalle-demo")
    .step(DalleProvider(), model="dall-e-3",
          prompt="A watercolor painting of a cozy bookshop on a rainy evening",
          modality=Modality.IMAGE, size="1024x1024", quality="hd")
    .run(timeout=120)
)
```

## Quickstart — OpenAI TTS

```python
from genblaze_openai import OpenAITTSProvider

run, manifest = (
    Pipeline("tts-demo")
    .step(OpenAITTSProvider(output_dir="output/audio"),
          model="tts-1-hd",
          prompt="Welcome to Genblaze — generative media pipelines with provenance.",
          modality=Modality.AUDIO, voice="nova", response_format="mp3")
    .run(timeout=60)
)
```

## Persist to Backblaze B2

```python
from genblaze_core import KeyStrategy, ObjectStorageSink
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)
# …then pass sink=storage to .run(…)
```

See [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) — the recommended default sink for genblaze.

## Credentials

| Env var | Where to get it |
|---|---|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Examples**: [`sora_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/sora_video_pipeline.py) · [`dalle_image_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/dalle_image_pipeline.py) · [`tts_audio_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/tts_audio_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends

## License

MIT
