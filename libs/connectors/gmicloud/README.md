<!-- last_verified: 2026-04-23 -->
# genblaze-gmicloud

**[GMICloud](https://gmicloud.ai) multi-provider video / image / audio adapters for [genblaze](https://github.com/backblaze-labs/genblaze) — access Seedance, Kling, Veo, Sora, Wan, Seedream, FLUX, Gemini image, ElevenLabs, MiniMax and more through one API with SHA-256 provenance manifests.**

`genblaze-gmicloud` wraps GMICloud's request-queue API, giving you one-call access to a large catalog of video, image, and audio models — including Kling, Veo, Sora, Wan, Seedream, FLUX-Kontext-Pro, Gemini-2.5-Flash-Image, ElevenLabs TTS, MiniMax TTS, and MiniMax Music — via three genblaze provider classes. Compose into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest on every run.

## Why genblaze-gmicloud

- **One API, dozens of models** — text-to-video (Seedance, Kling, Veo, Sora, Wan), text-to-image (Seedream, FLUX, Gemini, Reve), audio (ElevenLabs, MiniMax TTS/Music).
- **Provenance by default** — SHA-256-verified manifest with provider, model, prompt, params, cost.
- **Cost tracking** — `step.cost_usd` is populated from GMICloud's response.
- **Two auth modes** — API key (`GMI_API_KEY`) or SDK email/password.
- **Production-ready** — retries, timeouts, progress streaming, step caching.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Providers + models

| Provider class | Modality | Example models |
|---|---|---|
| `GMICloudVideoProvider` | video | `kling-text2video-v1.6-pro`, `kling-image2video-v2.1-master`, `veo3`, `wan2.6-t2v`, `seedance-1-0-pro-250528`, `sora-2-pro` |
| `GMICloudImageProvider` | image | `seedream-5.0-lite`, `gemini-2.5-flash-image`, `reve-edit-fast-20251030`, `flux-kontext-pro` |
| `GMICloudAudioProvider` | audio | `ElevenLabs-TTS-v3`, `MiniMax-TTS-Speech-2.6-Turbo`, `MiniMax-Music-2.5` |

Registered via entry points as `gmicloud`, `gmicloud-image`, and `gmicloud-audio`. Any model on GMICloud's queue is supported — pass the exact model slug.

> **Slug casing** — GMICloud's request queue is case-sensitive. Model ids are the lowercase slugs shown above. Pre-0.3 PascalCase ids (e.g. `Seedream-5.0-Lite`, `Veo3`, `Wan-2.6-I2V`) still resolve via `ModelSpec.deprecated_aliases` but emit a `DeprecationWarning` and will be removed in 0.4 — migrate early.

## Install

```bash
pip install genblaze-gmicloud
```

## Quickstart — video (Kling)

```bash
pip install genblaze-core genblaze-gmicloud
export GMI_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_gmicloud import GMICloudVideoProvider

run, manifest = (
    Pipeline("gmicloud-video-demo")
    .step(GMICloudVideoProvider(), model="kling-text2video-v1.6-pro",
          prompt="A drone shot flying over a misty mountain valley at sunrise, cinematic",
          modality=Modality.VIDEO, duration=10, aspect_ratio="16:9")
    .run(timeout=600)
)
print(run.steps[0].assets[0].url, f"${run.steps[0].cost_usd:.3f}")
```

## Quickstart — image (Seedream)

```python
from genblaze_gmicloud import GMICloudImageProvider

run, manifest = (
    Pipeline("gmicloud-image-demo")
    .step(GMICloudImageProvider(), model="seedream-5.0-lite",
          prompt="A photorealistic macro shot of morning dew on a spider web, soft bokeh",
          modality=Modality.IMAGE, aspect_ratio="16:9")
    .run(timeout=120)
)
```

## Quickstart — audio (ElevenLabs via GMICloud)

```python
from genblaze_gmicloud import GMICloudAudioProvider

run, manifest = (
    Pipeline("gmicloud-audio-demo")
    .step(GMICloudAudioProvider(), model="ElevenLabs-TTS-v3",
          prompt="Welcome to Genblaze — the fastest way to build generative AI pipelines.",
          modality=Modality.AUDIO)
    .run(timeout=120)
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
# pass sink=storage to .run(…)
```

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink — cost-efficient, S3-compatible, Object Lock for immutable manifests.

## Credentials

| Auth mode | Env var |
|---|---|
| API key (recommended) | `GMI_API_KEY` — https://console.gmicloud.ai/ |
| SDK email/password | `GMI_CLOUD_EMAIL` + `GMI_CLOUD_PASSWORD` |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Examples**: [`gmicloud_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/gmicloud_video_pipeline.py) · [`gmicloud_image_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/gmicloud_image_pipeline.py) · [`gmicloud_audio_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/gmicloud_audio_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends

## License

MIT
