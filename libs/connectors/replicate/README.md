<!-- last_verified: 2026-04-22 -->
# genblaze-replicate

**[Replicate](https://replicate.com) provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — run any Replicate-hosted image, video, or audio model through a unified AI pipeline with SHA-256 provenance.**

`genblaze-replicate` wraps Replicate's hosted model catalog (FLUX, SDXL, Stable Diffusion, video models, music models — anything on Replicate) as a genblaze provider. Compose Replicate calls into multi-step pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-replicate

- **Any Replicate model, one API** — Pass the `owner/model` slug; genblaze handles submission, polling, asset download, and manifest capture.
- **Provenance by default** — SHA-256 hash of every output, canonical manifest with prompt + params + timestamps.
- **Composable** — Chain Replicate outputs into downstream steps (image → video, upscale, transcode).
- **Production-ready** — Timeouts, retries, moderation hooks, step caching, OpenTelemetry tracing.
- **Durable storage** — Drop the `genblaze-s3` sink in for B2/S3/R2/MinIO persistence of assets + manifests.

## Models

Supports any model hosted on Replicate — including:

- **Image** — `black-forest-labs/flux-schnell`, `black-forest-labs/flux-dev`, `stability-ai/sdxl`, `stability-ai/stable-diffusion-3`
- **Video** — text-to-video and image-to-video models on Replicate
- **Audio** — music and speech models on Replicate

Use the full `owner/model` slug from https://replicate.com/explore.

## Install

```bash
pip install genblaze-replicate
```

Registers the `replicate` provider via entry points; [`genblaze-core`](https://pypi.org/project/genblaze-core/) discovers it automatically.

## Quickstart — FLUX Schnell (text-to-image)

```bash
pip install genblaze-core genblaze-replicate
export REPLICATE_API_TOKEN="r8_..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_replicate import ReplicateProvider

run, manifest = (
    Pipeline("flux-demo")
    .step(
        ReplicateProvider(),
        model="black-forest-labs/flux-schnell",
        prompt="a photorealistic golden retriever puppy in a field of wildflowers, "
               "golden hour, shallow depth of field",
        modality=Modality.IMAGE,
        num_outputs=1,
        aspect_ratio="1:1",
    )
    .run()
)

print(run.steps[0].assets[0].url)
print(manifest.canonical_hash)
assert manifest.verify()
```

## Quickstart — persist to Backblaze B2

```bash
pip install genblaze-core genblaze-replicate genblaze-s3
export REPLICATE_API_TOKEN="r8_..."
export B2_KEY_ID="..."  B2_APP_KEY="..."
```

```python
from genblaze_core import KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_replicate import ReplicateProvider
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
)

result = (
    Pipeline("flux-b2")
    .step(ReplicateProvider(), model="black-forest-labs/flux-schnell",
          prompt="cyberpunk tokyo street at night, neon reflections on wet pavement")
    .run(sink=storage, timeout=120)
)

print(result.run.steps[0].assets[0].url)   # durable B2 URL
```

## Credentials

| Env var | Where to get it |
|---|---|
| `REPLICATE_API_TOKEN` | https://replicate.com/account/api-tokens |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Runnable example**: [`replicate_flux_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/replicate_flux_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) and other S3-compatible backends

## License

MIT
