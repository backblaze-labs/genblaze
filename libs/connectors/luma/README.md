<!-- last_verified: 2026-04-22 -->
# genblaze-luma

**[Luma Dream Machine](https://lumalabs.ai/dream-machine) (Ray-2) video provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — text-to-video AI pipelines with SHA-256 provenance manifests on every render.**

`genblaze-luma` wraps Luma Labs' Dream Machine API (Ray-2, Ray-Flash-2) as a genblaze provider. Compose Luma video generations into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-luma

- **Luma Ray-2, unified API** — same `Pipeline` API as Sora, Veo, Runway, Flux.
- **Provenance by default** — SHA-256-verified manifest with prompt, model, and params on every render.
- **Production-ready** — timeouts, retries, progress streaming, moderation hooks, step caching.
- **Composable** — chain Luma outputs into downstream FFmpeg steps or AV compositors.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Models

| Model | Notes |
|---|---|
| `ray-2` | Latest, highest-quality Dream Machine |
| `ray-flash-2` | Faster and lower-cost variant |

## Install

```bash
pip install genblaze-luma
```

Registers the `luma` provider via entry points; [`genblaze-core`](https://pypi.org/project/genblaze-core/) discovers it automatically.

## Quickstart — Ray-2 text-to-video

```bash
pip install genblaze-core genblaze-luma
export LUMAAI_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_luma import LumaProvider

run, manifest = (
    Pipeline("luma-demo")
    .step(LumaProvider(), model="ray-2",
          prompt="A slow-motion shot of ocean waves crashing against volcanic rocks at golden hour, cinematic",
          modality=Modality.VIDEO, aspect_ratio="16:9")
    .run(timeout=300)
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

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for large AI-generated video.

## Credentials

| Env var | Where to get it |
|---|---|
| `LUMAAI_API_KEY` | https://lumalabs.ai/dream-machine/api |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Example**: [`luma_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/luma_video_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends
- Other video providers: [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) (Sora) · [`genblaze-google`](https://pypi.org/project/genblaze-google/) (Veo) · [`genblaze-runway`](https://pypi.org/project/genblaze-runway/) (Gen-4) · [`genblaze-decart`](https://pypi.org/project/genblaze-decart/) (Lucy)

## License

MIT
