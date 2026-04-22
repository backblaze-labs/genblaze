<!-- last_verified: 2026-04-22 -->
# genblaze-core

**Python SDK for building generative AI pipelines across video, image, and audio — with built-in SHA-256 provenance.**

`genblaze-core` is the core of [genblaze](https://github.com/backblaze-labs/genblaze), an open-source orchestration framework by [Backblaze](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) for composing multi-step AI media generation workflows. It gives you a single, provider-agnostic `Pipeline` API for text-to-video, text-to-image, text-to-speech, image-to-video, and audio generation — so you can swap models (Sora, Veo, Runway, Luma, Flux, DALL·E, ElevenLabs, Stable Audio, LMNT, GMICloud) without rewriting pipeline logic.

Every pipeline run emits a canonical, hash-verified **provenance manifest** — a tamper-evident JSON document capturing the provider, model, prompt, parameters, timestamps, and SHA-256 hash of every generated asset. Manifests can be embedded directly into PNG, JPEG, WebP, MP4, MP3, and WAV files, uploaded alongside assets to S3-compatible storage, or exported to Parquet for analytics.

## Why genblaze-core

- **One API for every generative AI provider** — Pipelines, not per-vendor SDK glue. Fluent, composable, chainable.
- **Built-in provenance** — Every asset gets a SHA-256–verified manifest. Prove how media was made; detect tampering.
- **Production-ready** — Retries, timeouts, progress streaming, moderation hooks, OpenTelemetry tracing, step caching.
- **Storage-agnostic sinks** — Drop into Backblaze B2, AWS S3, Cloudflare R2, MinIO, Parquet, or local disk.
- **Policy + privacy controls** — Redact prompts, strip params, pointer-mode for sensitive content.
- **Agent loops + templates** — Evaluator-driven iteration, reusable pipeline and step templates.
- **Zero lock-in** — MIT licensed, typed, lazy imports, provider adapters are separate packages.

## Features

| Capability | What you get |
|---|---|
| `Pipeline` API | Fluent multi-step generation, fan-in (`input_from`), AV compositing via FFmpeg |
| Provider discovery | Entry-point–based registry — `pip install genblaze-<provider>` and it's available |
| Manifest (Pydantic) | `Run`, `Step`, `Asset` models with canonical JSON hashing and `.verify()` |
| Media embedding | `PngHandler`, `Mp4Handler`, `Mp3Handler`, etc. — embed + extract manifests in-file |
| Storage sink | `ObjectStorageSink` with hierarchical or content-addressable key layout |
| Parquet sink | Partitioned run/step/asset tables for downstream analytics |
| Observability | `OTelTracer`, `LoggingTracer`, `CompositeTracer`, structured events |
| Agents | `AgentLoop` with pluggable `Evaluator` for iterative refinement |
| Moderation | Pre/post moderation hooks, configurable embed policies |
| Testing | `MockProvider`, `MockVideoProvider`, `MockAudioProvider` for offline tests |

## Install

```bash
pip install genblaze-core
```

Optional extras:

```bash
pip install "genblaze-core[parquet]"   # ParquetSink for analytics
pip install "genblaze-core[audio]"     # Audio metadata embedding (mutagen)
```

Add provider adapters separately:

```bash
pip install genblaze-openai genblaze-google genblaze-runway genblaze-luma \
            genblaze-decart genblaze-replicate genblaze-elevenlabs \
            genblaze-stability-audio genblaze-lmnt genblaze-gmicloud

pip install genblaze-s3    # Storage backend for Backblaze B2 / AWS S3 / R2 / MinIO
pip install genblaze-cli   # Extract / verify / replay / index manifests
```

## Quickstart — local, zero API keys

```python
from genblaze_core import Modality, Pipeline
from genblaze_core.testing import MockVideoProvider

run, manifest = (
    Pipeline("hello-genblaze")
    .step(MockVideoProvider(), model="mock-v1",
          prompt="A drone shot over a coastal city at golden hour",
          modality=Modality.VIDEO)
    .run()
)

print(manifest.canonical_hash)   # deterministic SHA-256 of the run
print(manifest.verify())         # True
```

## Quickstart — Sora + Backblaze B2 storage

Generate a video, upload it + its manifest to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze), verify the hash:

```bash
pip install genblaze-core genblaze-openai genblaze-s3
export OPENAI_API_KEY="sk-..."
export B2_KEY_ID="..."
export B2_APP_KEY="..."
```

```python
from genblaze_core import KeyStrategy, Modality, ObjectStorageSink, Pipeline
from genblaze_openai import SoraProvider
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

result = (
    Pipeline("hero-reel")
    .step(SoraProvider(), model="sora-2",
          prompt="Aerial flyover of a mountain lake at sunrise",
          modality=Modality.VIDEO, seconds=4, size="1280x720")
    .run(sink=storage, timeout=300)
)

print(result.run.steps[0].assets[0].url)   # durable B2 URL
print(result.manifest.canonical_hash)      # SHA-256 of the full run
assert result.manifest.verify()
```

## Storage — Backblaze B2 recommended

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for genblaze — purpose-built for large AI-generated media with S3-compatible APIs, resilient multipart uploads, Object Lock for immutable manifests, and strong cost economics at scale. One-liner credentials from `B2_KEY_ID` / `B2_APP_KEY`. See the [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) backend for the full recipe plus AWS S3, Cloudflare R2, and MinIO variants.

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Architecture**: https://github.com/backblaze-labs/genblaze/blob/main/ARCHITECTURE.md
- **Feature docs**: https://github.com/backblaze-labs/genblaze/tree/main/docs/features
- **Runnable examples**: https://github.com/backblaze-labs/genblaze/tree/main/examples

## Related packages

Provider adapters: [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) · [`genblaze-google`](https://pypi.org/project/genblaze-google/) · [`genblaze-runway`](https://pypi.org/project/genblaze-runway/) · [`genblaze-luma`](https://pypi.org/project/genblaze-luma/) · [`genblaze-decart`](https://pypi.org/project/genblaze-decart/) · [`genblaze-replicate`](https://pypi.org/project/genblaze-replicate/) · [`genblaze-elevenlabs`](https://pypi.org/project/genblaze-elevenlabs/) · [`genblaze-stability-audio`](https://pypi.org/project/genblaze-stability-audio/) · [`genblaze-lmnt`](https://pypi.org/project/genblaze-lmnt/) · [`genblaze-gmicloud`](https://pypi.org/project/genblaze-gmicloud/)

Storage + tooling: [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) · [`genblaze-cli`](https://pypi.org/project/genblaze-cli/) · [`genblaze-langsmith`](https://pypi.org/project/genblaze-langsmith/)

## License

MIT
