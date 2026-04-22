<!-- last_verified: 2026-04-22 -->
# genblaze-decart

**[Decart Lucy](https://platform.decart.ai) video + image provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — text-to-video, image-to-video, and image generation/editing AI pipelines with SHA-256 provenance manifests.**

`genblaze-decart` wraps Decart's Lucy family of models as genblaze providers — `DecartVideoProvider` for text-to-video and image-to-video, `DecartImageProvider` for text-to-image and image-to-image editing. Compose them into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-decart

- **Lucy Pro video + image + edits** — text-to-video, image-to-video, text-to-image, image-to-image, all under one pipeline API.
- **Chain Lucy I2V after Lucy T2I** — single pipeline, image→video with linked provenance.
- **Provenance by default** — SHA-256-verified manifest on every output.
- **Production-ready** — timeouts, retries, progress streaming, step caching.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Providers + models

| Provider class | Modality | Models |
|---|---|---|
| `DecartVideoProvider` | video | `lucy-pro-t2v` (text-to-video), `lucy-pro-i2v` / `lucy-dev-i2v` (image-to-video) |
| `DecartImageProvider` | image | `lucy-pro-t2i` (text-to-image), `lucy-pro-i2i` (image-to-image editing) |

Registered via entry points as `decart` and `decart-image`.

## Install

```bash
pip install genblaze-decart
```

## Quickstart — Lucy text-to-video

```bash
pip install genblaze-core genblaze-decart
export DECART_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_decart import DecartVideoProvider

run, manifest = (
    Pipeline("decart-demo")
    .step(DecartVideoProvider(output_dir="output/video"),
          model="lucy-pro-t2v",
          prompt="A serene ocean with dolphins jumping at sunset, cinematic lighting",
          modality=Modality.VIDEO, resolution="720p")
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

## Quickstart — Lucy image-to-video chain

```python
from genblaze_core import Modality, Pipeline
from genblaze_decart import DecartImageProvider, DecartVideoProvider

run, manifest = (
    Pipeline("image-to-video", chain=True)
    .step(DecartImageProvider(), model="lucy-pro-t2i",
          prompt="cyberpunk cityscape, neon reflections", modality=Modality.IMAGE)
    .step(DecartVideoProvider(), model="lucy-pro-i2v",
          prompt="camera slowly pans right", modality=Modality.VIDEO)
    .run(timeout=600)
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

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for large AI-generated video and image assets.

## Credentials

| Env var | Where to get it |
|---|---|
| `DECART_API_KEY` | https://platform.decart.ai/ |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Example**: [`decart_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/decart_video_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends
- Other video providers: [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) (Sora) · [`genblaze-google`](https://pypi.org/project/genblaze-google/) (Veo) · [`genblaze-runway`](https://pypi.org/project/genblaze-runway/) (Gen-4) · [`genblaze-luma`](https://pypi.org/project/genblaze-luma/) (Dream Machine)

## License

MIT
