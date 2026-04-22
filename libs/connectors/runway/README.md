<!-- last_verified: 2026-04-22 -->
# genblaze-runway

**[Runway](https://runwayml.com) Gen-4 / Gen-3 video provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — text-to-video AI pipelines with SHA-256 provenance manifests on every render.**

`genblaze-runway` wraps the [Runway ML API](https://dev.runwayml.com) (Gen-4 Turbo, Gen-3a Turbo) as a genblaze provider. Compose Runway video generations into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-runway

- **Runway Gen-4 Turbo, unified API** — same `Pipeline` API as Sora, Veo, Luma, Flux.
- **Provenance by default** — every render gets a SHA-256-verified manifest with prompt, model, params, timestamps.
- **Production-ready** — timeouts, retries, progress streaming, step caching, moderation hooks.
- **Composable** — chain Runway outputs into downstream FFmpeg transforms or AV compositors.
- **Durable storage** — drop the `genblaze-s3` sink in for B2 / AWS S3 / R2 / MinIO persistence.

## Models

| Model | Notes |
|---|---|
| `gen4_turbo` | Latest Runway video model — fast, highest quality |
| `gen3a_turbo` | Previous generation — still supported |

## Install

```bash
pip install genblaze-runway
```

Registers the `runway` provider via entry points; [`genblaze-core`](https://pypi.org/project/genblaze-core/) discovers it automatically.

## Quickstart — Gen-4 Turbo text-to-video

```bash
pip install genblaze-core genblaze-runway
export RUNWAYML_API_SECRET="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_runway import RunwayProvider

run, manifest = (
    Pipeline("runway-demo")
    .step(RunwayProvider(), model="gen4_turbo",
          prompt="A timelapse of wildflowers blooming in a meadow, soft morning light, macro detail",
          modality=Modality.VIDEO, duration=10)
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
assert manifest.verify()
```

## Persist to Backblaze B2

```python
from genblaze_core import KeyStrategy, ObjectStorageSink
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)
# pass sink=storage to .run(…) — assets + manifest uploaded to B2
```

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for large AI-generated video.

## Credentials

| Env var | Where to get it |
|---|---|
| `RUNWAYML_API_SECRET` | https://dev.runwayml.com/ |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Example**: [`runway_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/runway_video_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends
- Other video providers: [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) (Sora) · [`genblaze-google`](https://pypi.org/project/genblaze-google/) (Veo) · [`genblaze-luma`](https://pypi.org/project/genblaze-luma/) (Dream Machine) · [`genblaze-decart`](https://pypi.org/project/genblaze-decart/) (Lucy)

## License

MIT
