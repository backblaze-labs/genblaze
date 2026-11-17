<!-- last_verified: 2026-04-22 -->
# genblaze-openai

**OpenAI provider adapters for [genblaze](https://github.com/backblaze-labs/genblaze) — DALL·E / gpt-image text-to-image, TTS text-to-speech, and Sora-compatible video servers — with SHA-256 provenance manifests on every output.**

`genblaze-openai` wraps OpenAI's generative media APIs (DALL·E 3 and gpt-image-1 images, `tts-1` / `tts-1-hd` / `gpt-4o-mini-tts` audio) as genblaze providers, plus `SoraProvider` for OpenAI-compatible Videos API servers. Compose them into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-openai

- **Three modalities, one SDK** — image (DALL·E, gpt-image), audio (TTS), and video (Sora-compatible servers) — same `Pipeline` API.
- **Built-in provenance** — Every DALL·E image / TTS clip / video render lands with a SHA-256-verified manifest.
- **Swap models without rewrites** — Same pipeline works with Runway, Luma, Flux, Veo, ElevenLabs, etc.
- **Production-ready** — Retries, timeouts, moderation hooks, step caching, streaming events.
- **Durable storage** — Plug `genblaze-s3` in for B2 / AWS S3 / R2 / MinIO persistence.

## Providers + models

| Provider class | Modality | Models |
|---|---|---|
| `SoraProvider` | video | Slugs listed by your OpenAI-compatible server (OpenAI's own `sora-2` / `sora-2-pro` were retired 2026-09-24) |
| `DalleProvider` | image | `gpt-image-1`, `dall-e-3`, `dall-e-2` (+ edits) |
| `OpenAITTSProvider` | audio | `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts` |

Each is registered via entry points (`openai-sora`, `openai-dalle`, `openai-tts`).

## Install

```bash
pip install genblaze-openai
```

## Sora on OpenAI-compatible Videos API servers

OpenAI shut down Sora 2 and the Videos API on 2026-09-24. `SoraProvider` still
speaks that API, so it can drive any server that implements
`POST /videos`, `GET /videos/{id}` and `GET /videos/{id}/content`
(self-hosted vLLM-Omni or SGLang diffusion, or a hosted gateway). Pass
`base_url`, including the prefix the server mounts those routes under (usually
`/v1`); discovery then accepts the slugs the server lists in `/models`:

```python
from genblaze_core import Modality, Pipeline
from genblaze_openai import SoraProvider

provider = SoraProvider(api_key="...", base_url="http://localhost:8091/v1")
run, manifest = (
    Pipeline("compatible-video")
    .step(provider, model="your-video-model", prompt="A lighthouse at dusk",
          modality=Modality.VIDEO)
    .run(timeout=600)
)
```

`base_url` is keyword-only and must be `https://`, or `http://` on a loopback
host (localhost, 127.0.0.0/8 or ::1), with no credentials, query or fragment:
the API key is sent to it. Setting the openai SDK's `OPENAI_BASE_URL`
environment variable instead also works, but that value is used as-is and not
validated.
Valid `seconds` and `size` values depend on the server's model. The Sora
size/seconds checks apply only to `sora-*` slugs.

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
