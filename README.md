<!-- last_verified: 2026-04-16 -->
<h1 align="center" style="border-bottom: none">
    Genblaze
</h1>
<h2 align="center" style="border-bottom: none">
    Pipeline SDK for AI generated video, audio and images with built-in provenance.
</h2>


<div align="center">

[![PyPI](https://img.shields.io/pypi/v/genblaze-core)](https://pypi.org/project/genblaze-core/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![CI](https://github.com/backblaze-b2-samples/genblaze/actions/workflows/ci.yml/badge.svg)](https://github.com/backblaze-b2-samples/genblaze/actions/workflows/ci.yml)

</div>

Orchestration framework for generative media pipelines with built-in provenance tracking.

Genblaze embeds verifiable provenance metadata into video, audio, and image files automatically. Every output carries a SHA-256-verified manifest recording the model, prompt, and parameters used to create it — embedded directly into the media file or stored alongside it.

## Providers

genblaze ships with provider adapters for major generative AI platforms:

| | Video | Image | Audio |
|---|---|---|---|
| **OpenAI** | Sora | DALL-E / gpt-image-1 | TTS |
| **Google** | Veo | Imagen | — |
| **Runway** | Gen-4 Turbo | — | — |
| **Luma** | Dream Machine | — | — |
| **Decart** | Lucy | Lucy | — |
| **Replicate** | — | Flux, SDXL, etc. | — |
| **ElevenLabs** | — | — | TTS + Sound Effects |
| **Stability AI** | — | — | Stable Audio (music) |
| **LMNT** | — | — | TTS |
| **GMICloud** | Kling (via request queue) | — | — |

## Features

- **Pipeline API** — Fluent, composable multi-step generation pipelines with fan-in (`input_from`) and AV compositing
- **13 provider adapters** — OpenAI, Google, Runway, Luma, Decart, Replicate, ElevenLabs, Stability Audio, LMNT, GMICloud (across 11 connector packages)
- **Manifest provenance** — Every run produces a canonical, SHA-256-verified manifest
- **Media embedding** — Embed manifests into PNG, JPEG, WebP, MP4, MP3, WAV
- **S3-compatible storage** — Upload assets to Backblaze B2, AWS S3, Cloudflare R2, MinIO
- **Policy system** — Redact prompts, strip params, pointer mode for privacy control
- **Parquet sink** — Write structured run/step/asset data to partitioned Parquet
- **CLI toolkit** — Extract, verify, replay, and index manifests from the command line

## Install

```bash
pip install genblaze-core

# Add providers
pip install genblaze-openai      # OpenAI (Sora, DALL-E, TTS)
pip install genblaze-google      # Google (Veo, Imagen)
pip install genblaze-runway      # Runway Gen video
pip install genblaze-luma        # Luma Dream Machine video
pip install genblaze-decart      # Decart Lucy video/image
pip install genblaze-replicate   # Replicate (Flux, SDXL, etc.)
pip install genblaze-elevenlabs  # ElevenLabs TTS + sound effects
pip install genblaze-stability-audio  # Stability AI Stable Audio
pip install genblaze-lmnt        # LMNT fast TTS
pip install genblaze-gmicloud    # GMICloud (Kling video via request queue)

# Add storage + CLI
pip install genblaze-s3          # S3-compatible storage (B2, AWS, R2)
pip install genblaze-cli         # CLI tools
```

## Quickstart

Generate a video with OpenAI Sora — just one env var:

```bash
pip install genblaze-core genblaze-openai
export OPENAI_API_KEY=...
```

```python
from genblaze_core import Pipeline, Modality
from genblaze_openai import SoraProvider

run, manifest = (
    Pipeline("my-first-pipeline")
    .step(
        SoraProvider(),
        model="sora-2",
        prompt="A drone shot soaring over a coastal city at golden hour",
        modality=Modality.VIDEO,
    )
    .run(timeout=300)
)

print(f"Video:    {run.steps[0].assets[0].url}")
print(f"Hash:     {manifest.canonical_hash}")
print(f"Verified: {manifest.verify()}")
```

The manifest captures the full provenance chain — provider, model, prompt, parameters, timestamps, and a canonical hash for integrity verification.

> **No API key?** Try `examples/quickstart_local.py` — builds and verifies a manifest with zero external calls.

## Storage

Upload assets and manifests to any S3-compatible bucket with `sink=storage`. The sink handles asset transfer, manifest upload, and URL rewriting in a single operation.

```python
from genblaze_core import Pipeline, Modality, ObjectStorageSink, KeyStrategy
from genblaze_openai import SoraProvider
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend(bucket="my-bucket", endpoint_url="https://s3.us-west-004.backblazeb2.com"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

result = Pipeline("my-pipeline").step(
    SoraProvider(),
    model="sora-2",
    prompt="Aerial flyover of a mountain lake at sunrise",
    modality=Modality.VIDEO,
).run(sink=storage, timeout=300)

print(f"Asset URL: {result.run.steps[0].assets[0].url}")  # Points to your bucket
```

**Cloud + local embed** — upload to cloud, then embed provenance into the local copy:

```python
result = Pipeline("compose-demo").step(
    SoraProvider(),
    model="sora-2",
    prompt="Aerial flyover of a mountain lake at sunrise",
    modality=Modality.VIDEO,
).run(sink=storage, timeout=300)

# Embed the manifest into the local MP4
local_path = f"output/{result.run.steps[0].assets[0].asset_id}.mp4"
result.save(local_path)
```

**Cloud + Parquet analytics** — one sink does both:

```python
from genblaze_core import ParquetSink

storage = ObjectStorageSink(
    S3StorageBackend(bucket="my-bucket", endpoint_url="https://..."),
    key_strategy=KeyStrategy.HIERARCHICAL,
    parquet_sink=ParquetSink("data/"),  # Also write structured data locally
)

result = Pipeline("full-pipeline").step(...).run(sink=storage)
# Assets + manifest in bucket, run/step/asset tables in data/ as Parquet
```

**Bucket layouts:**

```
HIERARCHICAL (run-grouped):             CONTENT_ADDRESSABLE (deduped):
{prefix}/runs/                           {prefix}/assets/
  {tenant}/{date}/{run_id}/                {sha256[:2]}/{sha256[2:4]}/{sha256}.ext
    manifest.json                        {prefix}/manifests/
    assets/                                {run_id}.json
      {asset_id}.mp4
```

See [docs/features/object-storage.md](docs/features/object-storage.md) for full configuration reference.

## Iteration

Link runs together to track prompt refinement, parameter tuning, and forking:

```python
# First attempt
v1 = Pipeline("hero-video").step(
    SoraProvider(), model="sora-2",
    prompt="product reveal on dark background", modality=Modality.VIDEO,
).run(timeout=300)

# Refine — linked to v1 via parent_run_id
v2 = Pipeline("hero-video").from_result(v1).step(
    SoraProvider(), model="sora-2",
    prompt="product reveal on dark background, dramatic lighting, slow motion",
    modality=Modality.VIDEO,
).run(timeout=300)

# Fork into a different provider
v3 = Pipeline("hero-video-runway").from_result(v1).step(
    RunwayProvider(), model="gen4_turbo",
    prompt="slow zoom in on the product", modality=Modality.VIDEO,
).run(timeout=300)
```

Every manifest carries a `parent_run_id` pointer (excluded from the canonical hash). See [docs/features/iteration.md](docs/features/iteration.md).

### More examples

```python
# Multi-step: generate image then animate to video
from genblaze_core import Pipeline, Modality
from genblaze_openai import DalleProvider, SoraProvider

run, manifest = (
    Pipeline("image-to-video", chain=True)
    .step(DalleProvider(), model="dall-e-3", prompt="cyberpunk cityscape", modality=Modality.IMAGE)
    .step(SoraProvider(), model="sora-2", prompt="camera slowly pans right", modality=Modality.VIDEO)
    .run(timeout=300)
)

# Video with Luma Dream Machine
from genblaze_core import Pipeline, Modality
from genblaze_luma import LumaProvider

run, manifest = (
    Pipeline("luma-video")
    .step(LumaProvider(), model="ray-2", prompt="a cat playing piano", modality=Modality.VIDEO)
    .run(timeout=300)
)

# Generate speech with ElevenLabs
from genblaze_core import Pipeline, Modality
from genblaze_elevenlabs import ElevenLabsTTSProvider

run, manifest = (
    Pipeline("narration")
    .step(
        ElevenLabsTTSProvider(output_dir="output/"),
        model="eleven_v3",
        prompt="Welcome to the future of media provenance.",
        modality=Modality.AUDIO,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
    )
    .run()
)

# Generate music with Stability Audio
from genblaze_core import Pipeline, Modality
from genblaze_stability_audio import StabilityAudioProvider

run, manifest = (
    Pipeline("soundtrack")
    .step(
        StabilityAudioProvider(output_dir="output/"),
        model="stable-audio-2.5",
        prompt="Epic orchestral trailer music with rising tension",
        modality=Modality.AUDIO,
        duration=60,
    )
    .run(timeout=120)
)
```

### Embed manifest into media files

```python
from genblaze_core.media import Mp4Handler

handler = Mp4Handler()
handler.embed(mp4_path, manifest)

# Later, extract and verify
extracted = handler.extract(mp4_path)
assert extracted.verify()
```

### CLI

```bash
genblaze extract video.mp4          # Extract manifest from media
genblaze verify video.mp4           # Verify manifest integrity
genblaze replay manifest.json       # Preview a replay
genblaze index manifest.json -o ./  # Index into Parquet
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full system layout and data flows.

```
libs/spec/              # Language-neutral JSON Schemas (v1/)
libs/core/              # genblaze-core Python SDK
libs/connectors/        # Provider adapters (openai, google, runway, luma, ...)
cli/                    # CLI tool
examples/               # Usage examples
```

### Key concepts

| Concept | Description |
|---------|-------------|
| **Pipeline** | Fluent API for composing multi-step generation workflows |
| **Run** | A collection of generation steps forming a pipeline execution |
| **Step** | A single generation operation (generate, upscale, transcode) |
| **Asset** | A generated media artifact with URL, MIME type, and optional hash |
| **Manifest** | Hash-verified canonical JSON document capturing full provenance |
| **Provider** | Adapter implementing submit/poll/fetch_output for a generation API |
| **Sink** | Output destination for structured run data (Parquet, object storage) |

## Development

```bash
make install-dev    # Install all packages in dev mode
make test           # Run all tests (873 across 13 packages)
make lint           # Run ruff linter
make fmt            # Format code with ruff
make typecheck      # Run mypy type checker
make coverage       # Run tests with coverage
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — System layout, data flows, canonical files
- [docs/features/](docs/features/) — Feature docs (pipeline, providers, media, policy, sinks)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [AGENTS.md](AGENTS.md) for repo conventions.

## License

[MIT](LICENSE)
